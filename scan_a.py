"""A-share support/resistance scan — frozen spec v1.0 (SPEC_FROZEN_20261122.md).

Reads the frozen engine in the same directory (engine.py, read-only, never
modified here) and runs its analyze() on Sina daily K-lines.

Refresh contract (spec section 11, does not change zone rules):
  --date YYYY-MM-DD   explicit benchmark date (default 2026-11-20)
  --auto-date         use the last bar date of sh600519 Sina daily K-line
                      as the latest A-share trading day
  --out-dir           output dir (default: <script-dir>/output)
  --kline-dir         per-stock last-250-bar JSON dir
                      (default: <out-dir>/kline/a)
  --cache-dir         full-history CSV cache dir
                      (default: <out-dir>/kline_cache_a)
  --universe-source {snapshot_file,tencent_live}
                      snapshot_file: Tencent snapshot JSON (local only;
                      see --snapshot-file). tencent_live: fetch live
                      price/market-cap from qt.gtimg.cn for the code list
                      in --codes-file (the CI path: no snapshot file
                      exists on a GitHub Actions runner).
  --snapshot-file     Tencent snapshot JSON path (default: relative
                      ../valuation-screener/data/a_tencent_<date>.json
                      next to this script's directory; overridable)
  --codes-file        code list, one 6-digit code per line. In
                      snapshot_file mode it is WRITTEN (the filtered
                      universe). In tencent_live mode it is READ.
                      (default: <script-dir>/a_universe_codes.txt)
  --fresh             ignore checkpoint/partial results and refetch all
  --limit N           process only the first N universe codes (testing)
  --max-workers N     ThreadPoolExecutor workers (default 8)
  --datalen N         Sina K-line bars requested (default 600)

Paths are relative to this file / to CLI arguments only; no absolute
machine path is hard-coded, so the script runs from a repo root on a
GitHub Actions ubuntu runner. Dependencies: requests, pandas, numpy
(plus the frozen engine.py in the same directory).

Universe filters (sequential, any hit excludes; counts reconcile:
before - sum(excluded) = universe):
  1. Beijing Stock Exchange (bj prefix / 92xxxx) — not in scope
  2. STAR board: code starting 688
  3. name containing ST (covers ST / *ST / S*ST)
  4. total market cap < 50 (100M CNY) — Tencent raw field 45, unit 亿元
     (field 44 is circulating market cap; field 45 = price x total
     shares (field 73) / 1e8 — verified on 600519/601398 anchors)
  5. benchmark close < 5 CNY
  6. no trade that day: snapshot volume (field 6, hands) == 0
     (also covers suspended / delisted stale snapshot rows)

K-line validation (per stock, failure never stops the scan):
  last bar date (after truncating to <= benchmark date) must equal the
  benchmark date, and history must be >= 250 bars; otherwise the stock
  gets a failed record with an error reason. Fetch: 1 attempt + 3
  retries, exponential backoff, 30s timeout, max 8 threads.
Checkpointing: every 50 completed stocks the checkpoint file and a
partial-results file are written; a re-run resumes from them.
"""
import argparse
import csv
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import engine  # noqa: E402  (frozen engine, same directory)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BENCHMARK_DEFAULT = "2026-11-20"
SINA_URL = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData")
TENCENT_URL = "http://qt.gtimg.cn/q={codes}"
MIN_BARS = 250
CHECKPOINT_EVERY = 50
NEAR_PCT = 0.03


# ---------------------------------------------------------------- helpers

def compact(date_str):
    return date_str.replace("-", "")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="A-share SR scan (frozen v1.0)")
    p.add_argument("--date", default=None,
                   help="benchmark date YYYY-MM-DD (default 2026-11-20)")
    p.add_argument("--auto-date", action="store_true",
                   help="use sh600519 Sina last bar date as benchmark date")
    p.add_argument("--out-dir", default=None,
                   help="output dir (default <script-dir>/output)")
    p.add_argument("--kline-dir", default=None,
                   help="last-250-bar JSON dir (default <out-dir>/kline/a)")
    p.add_argument("--cache-dir", default=None,
                   help="full-history CSV cache dir (default <out-dir>/kline_cache_a)")
    p.add_argument("--universe-source", choices=["snapshot_file", "tencent_live"],
                   default="snapshot_file")
    p.add_argument("--snapshot-file", default=None,
                   help="Tencent snapshot JSON (snapshot_file mode)")
    p.add_argument("--codes-file", default=None,
                   help="code list file (written in snapshot mode, read in live mode)")
    p.add_argument("--fresh", action="store_true",
                   help="ignore checkpoint/partial results and refetch all")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-workers", type=int, default=8)
    p.add_argument("--datalen", type=int, default=600)
    return p.parse_args(argv)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sanitize(obj):
    """Convert numpy/pandas scalars so json.dumps works."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def to_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- universe

def tencent_fields(raw):
    """Split a Tencent qt raw string; returns dict of parsed fields."""
    parts = raw.split("~")
    if len(parts) < 46:
        return None
    return {
        "name": parts[1],
        "code": parts[2],
        "close": to_float(parts[3]),
        "volume_hands": to_float(parts[6]),
        # field 44 = circulating cap, field 45 = TOTAL cap (亿元)
        "market_cap_yi": to_float(parts[45]),
        "raw": raw,
    }


def apply_filters(entries):
    """entries: list of dicts with keys key/code/exchange/name/close/
    market_cap_yi/volume_hands. Sequential filters; returns
    (kept, counts dict, universe_before)."""
    counts = {"bj_excluded": 0, "star": 0, "st": 0, "cap_lt_50yi": 0,
              "price_lt_5": 0, "suspended": 0}
    kept = []
    for e in entries:
        if e["exchange"] == "BJ":
            counts["bj_excluded"] += 1
            continue
        if e["code"].startswith("688"):
            counts["star"] += 1
            continue
        if "ST" in e["name"]:
            counts["st"] += 1
            continue
        if e["market_cap_yi"] is None or e["market_cap_yi"] < 50:
            counts["cap_lt_50yi"] += 1
            continue
        if e["close"] is None or e["close"] < 5:
            counts["price_lt_5"] += 1
            continue
        if e["volume_hands"] is None or e["volume_hands"] == 0:
            counts["suspended"] += 1
            continue
        kept.append(e)
    return kept, counts, len(entries)


def build_universe_snapshot(snapshot_path, benchmark_date):
    if not snapshot_path.exists():
        raise FileNotFoundError(
            "snapshot file not found: %s (pass --snapshot-file or use "
            "--universe-source tencent_live)" % snapshot_path)
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    entries = []
    for key in sorted(data.keys()):
        rec = data[key]
        prefix = key[:2].lower()
        exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix, prefix.upper())
        f = tencent_fields(rec["raw"])
        if f is None:
            continue
        entries.append({
            "key": key, "code": f["code"], "exchange": exchange,
            "name": f["name"], "close": f["close"],
            "market_cap_yi": f["market_cap_yi"],
            "volume_hands": f["volume_hands"],
            "source": "tencent_snapshot:%s" % snapshot_path.name,
        })
    kept, counts, before = apply_filters(entries)
    return kept, counts, before


def fetch_tencent_live(codes, max_workers=8):
    """Batch-fetch qt.gtimg.cn quotes for 6-digit codes. Returns
    {code: fields-dict}. Codes needing a prefix: 6->sh, 0/3->sz, 9->bj."""
    from concurrent.futures import ThreadPoolExecutor

    def prefix(code):
        if code.startswith("6"):
            return "sh"
        if code.startswith("9"):
            return "bj"
        return "sz"

    def fetch_batch(batch):
        q = ",".join(prefix(c) + c for c in batch)
        last_err = None
        for attempt in range(4):
            try:
                r = requests.get(TENCENT_URL.format(codes=q), timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
                else:
                    raise last_err
        out = {}
        for m in re.finditer(r'v_(\w+)="([^"]*)"', r.text):
            key, raw = m.group(1), m.group(2)
            code = key[2:]
            f = tencent_fields(raw)
            if f is not None:
                out[code] = f
        return out

    batches = [codes[i:i + 100] for i in range(0, len(codes), 100)]
    result = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for out in ex.map(fetch_batch, batches):
            result.update(out)
    return result


def build_universe_live(codes_file, max_workers=8):
    if not codes_file.exists():
        raise FileNotFoundError(
            "codes file not found: %s (run snapshot_file mode first to "
            "generate it, or pass --codes-file)" % codes_file)
    codes = [ln.strip() for ln in codes_file.read_text(encoding="utf-8").splitlines()
             if ln.strip() and re.fullmatch(r"\d{6}", ln.strip())]
    live = fetch_tencent_live(codes, max_workers=max_workers)
    entries = []
    missing = 0
    for code in codes:
        f = live.get(code)
        if f is None:
            missing += 1
            continue
        exchange = "SH" if code.startswith("6") else ("BJ" if code.startswith("9") else "SZ")
        entries.append({
            "key": exchange.lower() + code, "code": code, "exchange": exchange,
            "name": f["name"], "close": f["close"],
            "market_cap_yi": f["market_cap_yi"],
            "volume_hands": f["volume_hands"],
            "source": "tencent_live:qt.gtimg.cn",
        })
    kept, counts, before = apply_filters(entries)
    counts["live_missing"] = missing
    # universe_before for live mode = codes queried (incl. missing)
    return kept, counts, len(codes)


# ---------------------------------------------------------------- K-line

def fetch_kline_sina(symbol, datalen=600):
    """Fetch Sina daily K-line. Returns list of bar dicts (ascending date).
    1 attempt + 3 retries, exponential backoff, 30s timeout."""
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)}
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(SINA_URL, params=params, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            payload = r.json()
            if not payload:
                raise ValueError("empty K-line response")
            bars = []
            for row in payload:
                bars.append({
                    "date": str(row["day"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(float(row["volume"])),
                })
            bars.sort(key=lambda b: b["date"])
            # de-duplicate dates, keep last
            dedup = {}
            for b in bars:
                dedup[b["date"]] = b
            return [dedup[d] for d in sorted(dedup)]
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError("K-line fetch failed after 3 retries: %s" % last_err)


def load_cache_csv(path):
    if not path.exists():
        return None
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append({
                "date": row["date"], "open": float(row["open"]),
                "high": float(row["high"]), "low": float(row["low"]),
                "close": float(row["close"]), "volume": int(float(row["volume"])),
            })
    rows.sort(key=lambda b: b["date"])
    return rows


def write_cache_files(code, name, bars, benchmark_date, kline_dir, cache_dir):
    """bars: truncated history (<= benchmark). CSV = full truncated
    history; JSON = last 250 bars for charting."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    with (cache_dir / ("%s.csv" % code)).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        for b in bars:
            w.writerow(b)
    kline_dir.mkdir(parents=True, exist_ok=True)
    last250 = bars[-250:]
    doc = {"symbol": code, "name": name, "benchmark_date": benchmark_date,
           "count": len(last250), "bars": last250}
    (kline_dir / ("%s.json" % code)).write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def analyze_bars(bars):
    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["date"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col])
    df = df.sort_values("date").reset_index(drop=True)
    return engine.analyze(df)


def nearest_distance(zones):
    if not zones:
        return None
    return min(z["distance"] for z in zones)


def build_record(entry, result, benchmark_date):
    """result: (kind, payload) where kind is 'ok' with engine output, or
    'fail' with an error string."""
    base = {
        "symbol": entry["code"],
        "name": entry["name"],
        "exchange": entry["exchange"],
        "market": "A",
        "close": entry["close"],
        "benchmark_date": benchmark_date,
        "market_cap_yi": (round(entry["market_cap_yi"], 2)
                          if entry["market_cap_yi"] is not None else None),
        "currency": "CNY",
        "supports": [], "resistances": [],
        "flipped_supports": [], "flipped_resistances": [],
        "nearest_support_distance": None,
        "nearest_resistance_distance": None,
        "in_support_danger": False,
        "in_resistance_danger": False,
        "has_valid_zone": False,
        "error": None,
    }
    kind, payload = result
    if kind == "fail":
        base["error"] = payload
        return base
    out = sanitize(payload)
    supports = out["supports"]
    resistances = out["resistances"]
    flip_sup = out["flipped_supports"]
    flip_res = out["flipped_resistances"]
    base.update({
        "close": round(float(out["close"]), 4),
        "supports": supports,
        "resistances": resistances,
        "flipped_supports": flip_sup,
        "flipped_resistances": flip_res,
        "nearest_support_distance": nearest_distance(supports + flip_sup),
        "nearest_resistance_distance": nearest_distance(resistances + flip_res),
        "in_support_danger": any(z["distance"] <= NEAR_PCT for z in supports + flip_sup),
        "in_resistance_danger": any(z["distance"] <= NEAR_PCT for z in resistances + flip_res),
        "has_valid_zone": bool(supports or resistances or flip_sup or flip_res),
        "error": None,
    })
    return base


def process_one(entry, benchmark_date, kline_dir, cache_dir, datalen, use_cache=True):
    """Fetch/validate/analyze one stock. Returns (record, last_date_or_None)."""
    code = entry["code"]
    symbol = ("sh" if code.startswith("6") else "sz") + code
    cache_path = cache_dir / ("%s.csv" % code)
    bars = None
    fetched = False
    if use_cache:
        try:
            cached = load_cache_csv(cache_path)
        except Exception:  # noqa: BLE001
            cached = None
        if cached:
            trunc = [b for b in cached if b["date"] <= benchmark_date]
            if trunc and trunc[-1]["date"] == benchmark_date and len(trunc) >= MIN_BARS:
                bars = trunc
    if bars is None:
        try:
            bars = fetch_kline_sina(symbol, datalen=datalen)
            fetched = True
        except Exception as exc:  # noqa: BLE001
            err = ("K-line fetch failed: %s；仅快照价、K线未取到" % exc)
            return build_record(entry, ("fail", err), benchmark_date), None
    trunc = [b for b in bars if b["date"] <= benchmark_date]
    if not trunc:
        err = "K-line has no bars on/before %s；close为快照价" % benchmark_date
        return build_record(entry, ("fail", err), benchmark_date), None
    last_date = trunc[-1]["date"]
    if last_date != benchmark_date:
        err = ("last K-line date %s != benchmark %s；close为快照价"
               % (last_date, benchmark_date))
        return build_record(entry, ("fail", err), benchmark_date), last_date
    if len(trunc) < MIN_BARS:
        err = ("only %d K-line bars (< %d)；close为快照价" % (len(trunc), MIN_BARS))
        return build_record(entry, ("fail", err), benchmark_date), last_date
    if fetched:
        write_cache_files(code, entry["name"], trunc, benchmark_date,
                          kline_dir, cache_dir)
    else:
        # cache hit: still ensure the chart JSON exists
        chart_path = kline_dir / ("%s.json" % code)
        if not chart_path.exists():
            write_cache_files(code, entry["name"], trunc, benchmark_date,
                              kline_dir, cache_dir)
    try:
        out = analyze_bars(trunc)
    except Exception as exc:  # noqa: BLE001
        err = "engine analyze failed: %s；close为快照价" % exc
        return build_record(entry, ("fail", err), benchmark_date), last_date
    return build_record(entry, ("ok", out), benchmark_date), last_date


# ---------------------------------------------------------------- main

def default_snapshot_path(benchmark_date):
    cands = [
        SCRIPT_DIR.parent / "valuation-screener" / "data" / ("a_tencent_%s.json" % compact(benchmark_date)),
        SCRIPT_DIR / "data" / ("a_tencent_%s.json" % compact(benchmark_date)),
        Path.cwd() / ("a_tencent_%s.json" % compact(benchmark_date)),
    ]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def main(argv=None):
    started = time.monotonic()
    args = parse_args(argv)
    date_given = args.date
    benchmark_date = date_given or BENCHMARK_DEFAULT

    out_dir = Path(args.out_dir) if args.out_dir else (SCRIPT_DIR / "output")
    kline_dir = Path(args.kline_dir) if args.kline_dir else (out_dir / "kline" / "a")
    cache_dir = Path(args.cache_dir) if args.cache_dir else (out_dir / "kline_cache_a")
    codes_file = Path(args.codes_file) if args.codes_file else (SCRIPT_DIR / "a_universe_codes.txt")
    out_dir.mkdir(parents=True, exist_ok=True)
    kline_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tag = compact(benchmark_date)

    if args.auto_date:
        bars519 = fetch_kline_sina("sh600519", datalen=args.datalen)
        if not bars519:
            raise RuntimeError("auto-date: empty K-line for sh600519")
        benchmark_date = bars519[-1]["date"]
        tag = compact(benchmark_date)
        print("auto-date: benchmark=%s" % benchmark_date, file=sys.stderr)

    # --- universe -----------------------------------------------------
    if args.universe_source == "snapshot_file":
        snapshot_path = (Path(args.snapshot_file) if args.snapshot_file
                         else default_snapshot_path(benchmark_date))
        universe, filt_counts, universe_before = build_universe_snapshot(
            snapshot_path, benchmark_date)
        universe_source_label = "tencent_snapshot"
        if snapshot_path.name != "a_tencent_%s.json" % tag:
            print("WARNING: snapshot file %s does not match benchmark date %s; "
                  "filtering uses the snapshot's own price/market-cap values."
                  % (snapshot_path.name, benchmark_date), file=sys.stderr)
    else:
        if date_given and not args.auto_date:
            # Live quotes can only serve the latest trading day.
            latest = fetch_kline_sina("sh600519", datalen=5)[-1]["date"]
            if benchmark_date != latest:
                raise RuntimeError(
                    "tencent_live cannot serve historical date %s "
                    "(latest trading day is %s); use --auto-date or "
                    "--universe-source snapshot_file" % (benchmark_date, latest))
        universe, filt_counts, universe_before = build_universe_live(
            codes_file, max_workers=args.max_workers)
        universe_source_label = "tencent_live"
    if args.limit:
        universe = universe[:args.limit]

    # universe outputs (date-stamped; codes file keeps its canonical name
    # for the frozen 2026-11-20 round, dated variant otherwise)
    universe_doc = [{
        "code": e["code"], "name": e["name"], "exchange": e["exchange"],
        "close": e["close"], "market_cap_yi": e["market_cap_yi"],
        "source": e["source"],
    } for e in universe]
    (out_dir / ("a_universe_%s.json" % tag)).write_text(
        json.dumps(universe_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    codes_out = codes_file if tag == "20261120" else (SCRIPT_DIR / ("a_universe_codes_%s.txt" % tag))
    if args.universe_source == "snapshot_file":
        codes_out.write_text("\n".join(e["code"] for e in universe) + "\n",
                             encoding="utf-8")
    print("universe: before=%d kept=%d counts=%s" % (universe_before, len(universe), filt_counts),
          file=sys.stderr)

    # --- resume state --------------------------------------------------
    checkpoint_path = out_dir / ("a_checkpoint_%s.json" % tag)
    partial_path = out_dir / ("a_scan_partial_%s.json" % tag)
    records = {}
    completed = []
    if not args.fresh and partial_path.exists():
        try:
            for rec in json.loads(partial_path.read_text(encoding="utf-8")):
                records[rec["symbol"]] = rec
            completed = list(records.keys())
            print("resume: %d completed from partial results" % len(records), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print("WARNING: partial results unreadable (%s); starting fresh" % exc,
                  file=sys.stderr)
            records, completed = {}, []
    if not args.fresh and checkpoint_path.exists() and not records:
        try:
            cp = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            completed = cp.get("completed_codes") or cp.get("completed") or []
        except Exception:  # noqa: BLE001
            completed = []
    completed_set = set(completed)
    todo = [e for e in universe if e["code"] not in completed_set]

    def save_progress():
        ordered = [records[e["code"]] for e in universe if e["code"] in records]
        partial_path.write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")
        cp = {"benchmark_date": benchmark_date,
              "completed": sorted(records.keys()),
              "completed_codes": sorted(records.keys()),
              "count": len(records),
              "timestamp": utc_now_iso(), "updated_at": utc_now_iso()}
        checkpoint_path.write_text(json.dumps(cp, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        if tag == "20261120":
            (out_dir / "a_checkpoint.json").write_text(
                checkpoint_path.read_text(encoding="utf-8"), encoding="utf-8")

    # --- scan -----------------------------------------------------------
    from concurrent.futures import ThreadPoolExecutor, as_completed

    latest_close_date = None
    done = len(records)
    if todo:
        with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futs = {ex.submit(process_one, e, benchmark_date, kline_dir,
                              cache_dir, args.datalen): e for e in todo}
            for fut in as_completed(futs):
                entry = futs[fut]
                try:
                    rec, last_date = fut.result()
                except Exception as exc:  # noqa: BLE001
                    rec = build_record(entry, ("fail", "unexpected error: %s；仅快照价、K线未取到" % exc),
                                       benchmark_date)
                    last_date = None
                records[entry["code"]] = rec
                if rec["error"] is None and last_date:
                    latest_close_date = last_date
                done += 1
                if done % CHECKPOINT_EVERY == 0 or done == len(universe):
                    save_progress()
                    print("progress %d/%d" % (done, len(universe)), file=sys.stderr)
        save_progress()
    else:
        latest_close_date = benchmark_date if records else None

    ordered_records = [records[e["code"]] for e in universe]
    success = sum(1 for r in ordered_records if r["error"] is None)
    failed = len(ordered_records) - success
    if latest_close_date is None and success:
        latest_close_date = benchmark_date

    # --- outputs ---------------------------------------------------------
    scan_json = out_dir / ("a_scan_%s.json" % tag)
    scan_json.write_text(json.dumps(ordered_records, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    fieldnames = ["symbol", "name", "exchange", "market", "close", "benchmark_date",
                  "market_cap_yi", "currency", "supports", "resistances",
                  "flipped_supports", "flipped_resistances",
                  "nearest_support_distance", "nearest_resistance_distance",
                  "in_support_danger", "in_resistance_danger", "has_valid_zone", "error"]
    scan_csv = out_dir / ("a_scan_%s.csv" % tag)
    with scan_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in ordered_records:
            row = dict(r)
            for k in ("supports", "resistances", "flipped_supports", "flipped_resistances"):
                row[k] = json.dumps(r[k], ensure_ascii=False)
            w.writerow(row)

    elapsed = round(time.monotonic() - started, 1)
    failed_symbols = [{"symbol": r["symbol"], "reason": r["error"]}
                      for r in ordered_records if r["error"] is not None]
    summary = {
        "universe_before": universe_before,
        "filtered_counts": filt_counts,
        "universe_source": universe_source_label,
        "scanned": len(ordered_records),
        "success": success,
        "failed": failed,
        "failed_symbols": failed_symbols,
        "valid_zone_count": sum(1 for r in ordered_records if r["has_valid_zone"]),
        "no_zone_count": sum(1 for r in ordered_records if not r["has_valid_zone"] and r["error"] is None),
        "support_danger_count": sum(1 for r in ordered_records if r["in_support_danger"]),
        "resistance_danger_count": sum(1 for r in ordered_records if r["in_resistance_danger"]),
        "elapsed_seconds": elapsed,
        "benchmark_date": benchmark_date,
    }
    (out_dir / "a_scan_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if tag != "20261120":
        (out_dir / ("a_scan_summary_%s.json" % tag)).write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    manifest = {
        "snapshot_date": benchmark_date,
        "generated_at_utc": utc_now_iso(),
        "market": "A",
        "latest_close_date": latest_close_date,
        "universe_count": len(ordered_records),
        "success": success,
        "failed": failed,
        "success_rate": round(success / len(ordered_records), 4) if ordered_records else 0.0,
        "data_sources": (["Tencent snapshot", "Sina K-line"] if universe_source_label == "tencent_snapshot"
                         else ["Tencent live", "Sina K-line"]),
    }
    (out_dir / "a_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    if tag != "20261120":
        (out_dir / ("a_manifest_%s.json" % tag)).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    print("done: scanned=%d success=%d failed=%d elapsed=%ss" % (len(ordered_records), success, failed, elapsed),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
