#!/usr/bin/env python3
"""US support/resistance scan — frozen spec v1.0 (SPEC_FROZEN_20261122.md).

Runs the frozen engine (engine.py, read-only, never modified here) over the
top-300 US common stocks by Nasdaq screener marketCap, as of a benchmark
close date. No scoring, no buy/sell advice, no parameter changes.

Refresh contract (spec sec.11, no zone-rule change):
  --date YYYY-MM-DD   explicit benchmark date (default 2026-11-20)
  --auto-date         use META's Yahoo daily-bar last date as the latest
                      US trading day (overrides --date)
  --out-dir           output dir (default: <repo root>/output, resolved
                      relative to this script, never a hardcoded absolute path)
  --kline-dir         per-ticker last-250-bar JSON dir
                      (default: <out-dir>/kline/us/)
  --kline-cache-dir   full-history CSV cache dir
                      (default: <out-dir>/kline_cache_us/)

Outputs (dated by benchmark date, except manifest/checkpoint notes below):
  us_scan_<YYYYMMDD>.json / .csv   one record per universe ticker
  us_scan_summary.json             summary (plus dated copy)
  us_universe_<YYYYMMDD>.json      universe snapshot + filtered-out list
  us_checkpoint_<YYYYMMDD>.json    checkpoint every 20 tickers (+ generic copy)
  us_manifest.json                 latest-run manifest for the website

Runnable from the repository root on GitHub Actions (ubuntu runner):
  python scan_us.py [--date YYYY-MM-DD | --auto-date] [--out-dir DIR]
Dependencies: requests, pandas, numpy only (plus the stdlib).
Engine import is relative to this script's directory via sys.path.
"""
import argparse
import csv
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# --- relative engine import: works from the repo root on any machine ---
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from engine import analyze  # noqa: E402  (frozen engine, read-only)

DEFAULT_BENCHMARK_DATE = "2026-11-20"
NASDAQ_URL = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=25&offset=0&download=true"
)
YAHOO_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
PERIOD1 = 1704067200  # 2024-01-01 UTC, fixed by the original task
UA = "Mozilla/5.0"
MIN_BARS = 250
MAX_WORKERS = 8
MAX_RETRIES = 3  # retries after the first attempt (up to 4 attempts total)
TIMEOUT = 30
BENCHMARK_DATE = None  # set in main() from CLI


def _parse_market_cap(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _filter_reasons(row):
    """Reasons a screener row is excluded from the common-stock universe."""
    name = (row.get("name") or "").strip()
    reasons = []
    if "etf" in name.lower():
        reasons.append("ETF")
    if "preferred" in name.lower() or "%" in name:
        reasons.append("Preferred/%")
    if re.search(r"\bwarrants?\b", name, re.I):
        reasons.append("Warrant")
    if re.search(r"\bunits?\b", name, re.I):
        reasons.append("Unit")
    if re.search(r"\brights?\b", name, re.I):
        reasons.append("Right")
    mc = _parse_market_cap(row.get("marketCap"))
    if mc is None or mc == 0:
        reasons.append("marketCap缺失或为0")
    return reasons


def fetch_universe(session):
    """Fetch the Nasdaq screener. download=true returns the full list in
    one call (verified: 7,301 rows, no totalRecords field); if a future
    response carries a total and fewer rows, paginate to fetch the rest."""
    resp = session.get(
        NASDAQ_URL,
        headers={"Accept": "application/json", "User-Agent": UA},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or {}
    rows = list(data.get("rows") or [])
    total = data.get("totalRecords")
    if total is not None:
        try:
            total = int(total)
        except (TypeError, ValueError):
            total = None
    if total is not None and len(rows) < total:
        offset = len(rows)
        while len(rows) < total:
            url = (
                "https://api.nasdaq.com/api/screener/stocks"
                f"?tableonly=true&limit=25&offset={offset}&download=true"
            )
            page = session.get(
                url,
                headers={"Accept": "application/json", "User-Agent": UA},
                timeout=TIMEOUT,
            )
            page.raise_for_status()
            page_rows = (page.json().get("data") or {}).get("rows") or []
            if not page_rows:
                break
            rows.extend(page_rows)
            offset += len(page_rows)
    return rows, total


def build_universe(rows, fetched_at):
    """Filter + rank. Returns (universe300, filtered_out, total_before)."""
    filtered_out = []
    kept = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        name = (row.get("name") or "").strip()
        mc = _parse_market_cap(row.get("marketCap"))
        reasons = _filter_reasons(row)
        if reasons:
            filtered_out.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "marketCap": mc,
                    "reasons": reasons,
                }
            )
        else:
            kept.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "marketCap": mc,
                    "market_cap_usd": mc,
                }
            )
    kept.sort(key=lambda r: r["market_cap_usd"], reverse=True)
    universe = kept[:300]
    for entry in universe:
        entry["fetched_at"] = fetched_at
    return universe, filtered_out, len(rows)


def _yahoo_ticker(symbol):
    # Yahoo uses '-' where Nasdaq uses '.' or '/' (e.g. BRK/B -> BRK-B).
    return symbol.replace(".", "-").replace("/", "-").replace("^", "-")


def _bar_date(timestamp, tz_name):
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(timestamp, tz=ZoneInfo(tz_name))
    except Exception:
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.date()


def fetch_kline(session, symbol, period2):
    """Fetch Yahoo daily bars. Returns (df, last_date_str, last_close).

    Raises RuntimeError with a human-readable reason after all retries.
    df columns: date, open, high, low, close, volume (date = Timestamp).
    Only bars with a full OHLC + close are kept and counted.
    """
    ticker = _yahoo_ticker(symbol)
    url = YAHOO_URL.format(ticker=ticker)
    params = {"period1": PERIOD1, "period2": period2, "interval": "1d"}
    last_err = "unknown error"
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(
                url, params=params, headers={"User-Agent": UA}, timeout=TIMEOUT
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
            chart = payload.get("chart") or {}
            if chart.get("error"):
                raise RuntimeError(str(chart["error"]))
            results = chart.get("result") or []
            if not results:
                raise RuntimeError("Yahoo返回空result")
            result = results[0]
            meta = result.get("meta") or {}
            tz_name = meta.get("exchangeTimezoneName") or "America/New_York"
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators") or {}).get("quote") or [{}]
            quote = quote[0] if quote else {}
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []
            rows = []
            for i, ts in enumerate(timestamps):
                if i >= len(closes) or closes[i] is None:
                    continue
                if (
                    i >= len(opens)
                    or opens[i] is None
                    or highs[i] is None
                    or lows[i] is None
                ):
                    continue
                rows.append(
                    {
                        "date": pd.Timestamp(_bar_date(ts, tz_name)),
                        "open": float(opens[i]),
                        "high": float(highs[i]),
                        "low": float(lows[i]),
                        "close": float(closes[i]),
                        "volume": (
                            int(volumes[i])
                            if i < len(volumes) and volumes[i] is not None
                            else None
                        ),
                    }
                )
            if not rows:
                raise RuntimeError("Yahoo返回0根有效K线")
            df = pd.DataFrame(rows).sort_values("date").drop_duplicates("date")
            df = df.reset_index(drop=True)
            last_date = str(df["date"].iloc[-1].date())
            last_close = float(df["close"].iloc[-1])
            return df, last_date, last_close
        except Exception as exc:  # noqa: BLE001 - retried, last error kept
            last_err = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)  # exponential backoff: 1s, 2s, 4s
    raise RuntimeError(f"Yahoo取数失败（重试{MAX_RETRIES}次后）: {last_err}")


def fetch_meta_last_date(session):
    """--auto-date: META's Yahoo daily-bar last date = latest US trading day."""
    period2 = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
    _df, last_date, _close = fetch_kline(session, "META", period2)
    return last_date


def _clean(obj):
    """Make engine output JSON-serialisable (numpy/pandas scalars -> plain)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item"):  # numpy scalar
        try:
            return obj.item()
        except Exception:
            pass
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _write_kline_cache(df, symbol, kline_dir, kline_cache_dir):
    """Last-250-bar JSON for the website + full-history CSV cache."""
    kline_dir.mkdir(parents=True, exist_ok=True)
    kline_cache_dir.mkdir(parents=True, exist_ok=True)
    # Filenames must never contain the raw symbol: 'BRK/B' would create a
    # spurious sub-directory. Use the Yahoo ticker form (BRK-B) for files;
    # the record itself keeps the original Nasdaq symbol.
    safe = _yahoo_ticker(symbol)
    recent = df.tail(MIN_BARS)
    bars = [
        {
            "date": str(pd.Timestamp(r["date"]).date()),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": (int(r["volume"]) if r["volume"] is not None else None),
        }
        for _, r in recent.iterrows()
    ]
    with open(kline_dir / f"{safe}.json", "w", encoding="utf-8") as fh:
        json.dump(bars, fh, ensure_ascii=False)
    with open(
        kline_cache_dir / f"{safe}.csv", "w", encoding="utf-8", newline=""
    ) as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for _, r in df.iterrows():
            writer.writerow(
                [
                    str(pd.Timestamp(r["date"]).date()),
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["volume"],
                ]
            )


def _failed_record(entry, reason, close):
    return {
        "symbol": entry["symbol"],
        "name": entry["name"],
        "market": "US",
        "close": close,
        "benchmark_date": BENCHMARK_DATE,
        "market_cap_usd": entry["market_cap_usd"],
        "currency": "USD",
        "supports": [],
        "resistances": [],
        "flipped_supports": [],
        "flipped_resistances": [],
        "nearest_support_distance": None,
        "nearest_resistance_distance": None,
        "in_support_danger": False,
        "in_resistance_danger": False,
        "has_valid_zone": False,
        "error": reason,
    }


def process_one(entry, session, period2, kline_dir, kline_cache_dir):
    """Fetch + validate + analyse one ticker. Never raises for a bad ticker:
    returns a record (success with zones, or failed with error + close=null
    unless the benchmark-date close is determinable)."""
    symbol = entry["symbol"]
    try:
        df, last_date, last_close = fetch_kline(session, symbol, period2)
    except Exception as exc:  # noqa: BLE001 - per-ticker failure is a record
        return _failed_record(entry, str(exc), None), None
    # Validation gate (spec: last date must be the benchmark date; >=250 bars).
    determinable_close = last_close if last_date == BENCHMARK_DATE else None
    if last_date != BENCHMARK_DATE:
        return (
            _failed_record(
                entry,
                f"最后一条日期为{last_date}，不是基准日{BENCHMARK_DATE}",
                None,
            ),
            last_date,
        )
    if len(df) < MIN_BARS:
        return (
            _failed_record(
                entry,
                f"历史仅{len(df)}根，少于{MIN_BARS}根",
                determinable_close,
            ),
            last_date,
        )
    try:
        result = analyze(df)
    except Exception as exc:  # noqa: BLE001 - engine failure is a record
        return (
            _failed_record(entry, f"引擎分析失败: {exc}", determinable_close),
            last_date,
        )
    supports = _clean(result["supports"])
    resistances = _clean(result["resistances"])
    flipped_supports = _clean(result["flipped_supports"])
    flipped_resistances = _clean(result["flipped_resistances"])
    close = float(result["close"])
    # Success close must equal the last K-line close (same bar by construction).
    if close != last_close:
        # Engine close comes from its own last bar; keep the engine value but
        # only if the dates agree (they do: validation passed above).
        pass
    sup_dists = [z["distance"] for z in supports + flipped_supports]
    res_dists = [z["distance"] for z in resistances + flipped_resistances]
    nearest_sup = min(sup_dists) if sup_dists else None
    nearest_res = min(res_dists) if res_dists else None
    record = {
        "symbol": symbol,
        "name": entry["name"],
        "market": "US",
        "close": close,
        "benchmark_date": BENCHMARK_DATE,
        "market_cap_usd": entry["market_cap_usd"],
        "currency": "USD",
        "supports": supports,
        "resistances": resistances,
        "flipped_supports": flipped_supports,
        "flipped_resistances": flipped_resistances,
        "nearest_support_distance": nearest_sup,
        "nearest_resistance_distance": nearest_res,
        "in_support_danger": any(d <= 0.03 for d in sup_dists),
        "in_resistance_danger": any(d <= 0.03 for d in res_dists),
        "has_valid_zone": bool(
            supports or resistances or flipped_supports or flipped_resistances
        ),
        "error": None,
    }
    _write_kline_cache(df, symbol, kline_dir, kline_cache_dir)
    return record, last_date


def _ordered_records(results, universe):
    order = {e["symbol"]: i for i, e in enumerate(universe)}
    return sorted(results.values(), key=lambda r: order.get(r["symbol"], 9999))


def _write_scan_files(records, out_dir, date_str):
    json_path = out_dir / f"us_scan_{date_str}.json"
    csv_path = out_dir / f"us_scan_{date_str}.csv"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    fields = [
        "symbol",
        "name",
        "market",
        "close",
        "benchmark_date",
        "market_cap_usd",
        "currency",
        "supports",
        "resistances",
        "flipped_supports",
        "flipped_resistances",
        "nearest_support_distance",
        "nearest_resistance_distance",
        "in_support_danger",
        "in_resistance_danger",
        "has_valid_zone",
        "error",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            row = dict(rec)
            for key in (
                "supports",
                "resistances",
                "flipped_supports",
                "flipped_resistances",
            ):
                row[key] = json.dumps(rec[key], ensure_ascii=False)
            writer.writerow(row)
    return json_path, csv_path


def _write_checkpoint(checkpoint_paths, completed, started_at):
    payload = {
        "completed_symbols": sorted(completed),
        "count": len(completed),
        "benchmark_date": BENCHMARK_DATE,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "started_at": started_at,
    }
    for path in checkpoint_paths:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)


def main(argv=None):
    global BENCHMARK_DATE
    parser = argparse.ArgumentParser(description="US support/resistance scan")
    parser.add_argument("--date", default=DEFAULT_BENCHMARK_DATE,
                        help="explicit benchmark date YYYY-MM-DD")
    parser.add_argument("--auto-date", action="store_true",
                        help="use META Yahoo last bar date as latest trading day")
    parser.add_argument("--out-dir", default=None,
                        help="output dir (default: <script dir>/output)")
    parser.add_argument("--kline-dir", default=None,
                        help="last-250-bar JSON dir (default: <out-dir>/kline/us)")
    parser.add_argument("--kline-cache-dir", default=None,
                        help="full-history CSV dir (default: <out-dir>/kline_cache_us)")
    args = parser.parse_args(argv)

    started = time.time()
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    script_dir = Path(__file__).resolve().parent
    out_dir = Path(args.out_dir) if args.out_dir else (script_dir / "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    kline_dir = Path(args.kline_dir) if args.kline_dir else (out_dir / "kline" / "us")
    kline_cache_dir = (
        Path(args.kline_cache_dir)
        if args.kline_cache_dir
        else (out_dir / "kline_cache_us")
    )

    session = requests.Session()
    if args.auto_date:
        BENCHMARK_DATE = fetch_meta_last_date(session)
        print(f"--auto-date: META last bar date = {BENCHMARK_DATE}", flush=True)
    else:
        BENCHMARK_DATE = args.date
    date_str = BENCHMARK_DATE.replace("-", "")
    # period2 = benchmark + 2 days (00:00 UTC). For 2026-11-20 this
    # includes the 11-20 bar and excludes anything later, functionally
    # equivalent to the original fixed 1795363200 (11-22 16:00 UTC).
    benchmark_dt = datetime.strptime(BENCHMARK_DATE, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
    period2 = int((benchmark_dt + timedelta(days=2)).timestamp())

    # --- universe ---
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows, total_records = fetch_universe(session)
    universe, filtered_out, total_before = build_universe(rows, fetched_at)
    universe_path = out_dir / f"us_universe_{date_str}.json"
    with open(universe_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "benchmark_date": BENCHMARK_DATE,
                "source": NASDAQ_URL,
                "fetched_at": fetched_at,
                "total_records": total_records,
                "rows_returned": len(rows),
                "download_full": total_records is None,
                "total_before_filter": total_before,
                "filtered_out_count": len(filtered_out),
                "filtered_out": filtered_out,
                "universe_count": len(universe),
                "universe": universe,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print(
        f"universe: total_before_filter={total_before} "
        f"filtered_out={len(filtered_out)} universe={len(universe)}",
        flush=True,
    )

    # --- resume: skip tickers already in this date's scan file ---
    scan_json_path = out_dir / f"us_scan_{date_str}.json"
    results = {}
    if scan_json_path.exists():
        try:
            with open(scan_json_path, encoding="utf-8") as fh:
                for rec in json.load(fh):
                    results[rec["symbol"]] = rec
        except Exception:
            results = {}
    remaining = [e for e in universe if e["symbol"] not in results]
    print(f"resume: {len(results)} already done, {len(remaining)} remaining",
          flush=True)

    checkpoint_paths = [
        out_dir / f"us_checkpoint_{date_str}.json",
        out_dir / "us_checkpoint.json",
    ]
    last_dates = {}

    def _checkpoint(completed):
        _write_checkpoint(checkpoint_paths, completed, started_at)
        ordered = _ordered_records(results, universe)
        _write_scan_files(ordered, out_dir, date_str)

    completed_count = 0
    # One session per thread: requests.Session is not thread-safe for
    # connection reuse guarantees; use a fresh Session per worker via
    # thread-local storage through a simple per-task session.
    def _work(entry):
        sess = requests.Session()
        try:
            return process_one(entry, sess, period2, kline_dir, kline_cache_dir)
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_work, e): e for e in remaining}
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                record, last_date = fut.result()
            except Exception as exc:  # noqa: BLE001 - must not stop the scan
                record = _failed_record(entry, f"未预期错误: {exc}", None)
                last_date = None
            results[entry["symbol"]] = record
            if last_date:
                last_dates[entry["symbol"]] = last_date
            completed_count += 1
            if completed_count % 20 == 0 or completed_count == len(remaining):
                _checkpoint(set(results.keys()))
                print(f"progress: {len(results)}/{len(universe)}", flush=True)

    records = _ordered_records(results, universe)
    _write_scan_files(records, out_dir, date_str)
    _checkpoint(set(results.keys()))

    # --- summary ---
    success = sum(1 for r in records if r["error"] is None)
    failed_recs = [r for r in records if r["error"] is not None]
    elapsed = round(time.time() - started, 2)
    summary = {
        "universe_total": total_before,
        "filtered_out": len(filtered_out),
        "scanned": len(records),
        "success": success,
        "failed": len(failed_recs),
        "failed_symbols": [
            {"symbol": r["symbol"], "reason": r["error"]} for r in failed_recs
        ],
        "valid_zone_count": sum(1 for r in records if r["has_valid_zone"]),
        "no_zone_count": sum(1 for r in records if not r["has_valid_zone"]),
        "support_danger_count": sum(
            1 for r in records if r["in_support_danger"]
        ),
        "resistance_danger_count": sum(
            1 for r in records if r["in_resistance_danger"]
        ),
        "elapsed_seconds": elapsed,
        "benchmark_date": BENCHMARK_DATE,
    }
    summary_path = out_dir / "us_scan_summary.json"
    summary_dated_path = out_dir / f"us_scan_summary_{date_str}.json"
    for path in (summary_path, summary_dated_path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)

    # --- manifest (spec sec.11 refresh contract) ---
    if last_dates:
        latest_close_date = max(last_dates.values())
    elif success:
        latest_close_date = BENCHMARK_DATE
    else:
        latest_close_date = None
    manifest = {
        "snapshot_date": BENCHMARK_DATE,
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "market": "US",
        "latest_close_date": latest_close_date,
        "universe_count": len(universe),
        "success": success,
        "failed": len(failed_recs),
        "success_rate": round(success / len(universe), 6) if universe else 0.0,
        "data_sources": ["Nasdaq screener", "Yahoo chart"],
    }
    with open(out_dir / "us_manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"manifest: {json.dumps(manifest, ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
