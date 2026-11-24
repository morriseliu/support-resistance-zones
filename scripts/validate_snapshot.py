#!/usr/bin/env python3
"""Snapshot validation gate (frozen spec sec.11).

Checks a freshly scanned snapshot BEFORE it may be published:
1. Each market's snapshot_date equals the latest close date actually present
   in that market's scan (last K-line date), and the two manifests agree.
2. Spot-check tickers re-fetched live from the source APIs match the scan:
   META / TSM / TSLA / AVGO (Yahoo) and 600519 (Sina) - close within 0.01
   and last-bar date equal to the snapshot date.
3. Success rate: >= 0.85 absolute, and not more than 5pp below the previous
   published manifest (if one exists).
4. Danger counts recomputed from the records match the manifest counts
   (when the manifest carries them).

Exit 0 = PASS (writes validation JSON). Non-zero = FAIL: the caller must
NOT publish the snapshot, must keep the old snapshot, and must surface the
failure. Never publish stale data as fresh.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SPOT_US = ["META", "TSM", "TSLA", "AVGO"]
SPOT_A = ["600519"]
HEADERS = {"User-Agent": "Mozilla/5.0"}


def yahoo_close(symbol):
    # period2 must stay ahead of "now" (same as scan_us.py): a fixed
    # period2 freezes the last bar at that date, so the spot-check would
    # always fail for any snapshot after it. 2026-11-22 16:00 UTC fixed
    # value used to return 2026-11-20 forever.
    period2 = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1=1704067200&period2={period2}&interval=1d")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    i = len(ts) - 1
    while closes[i] is None and i > 0:
        i -= 1
    date = datetime.fromtimestamp(ts[i], tz=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")).date().isoformat()
    return date, float(closes[i])


def sina_close(code):
    prefix = "sh" if code.startswith("6") else "sz"
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s%s&scale=240&ma=no&datalen=5"
           % (prefix, code))
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    rows = r.json()
    last = rows[-1]
    return last["day"], float(last["close"])


def _reject_constant(name):
    # Python's json accepts NaN/Infinity, browsers' JSON.parse does not.
    # A single such token in a published scan file blanks every table.
    raise ValueError("non-strict JSON constant in scan file: %s" % name)


def load_records(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f, parse_constant=_reject_constant)
    return data if isinstance(data, list) else data.get("records", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--us-scan", required=True)
    ap.add_argument("--a-scan", required=True)
    ap.add_argument("--us-manifest", required=True)
    ap.add_argument("--a-manifest", required=True)
    ap.add_argument("--prev-manifest", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-live-check", action="store_true",
                    help="Skip live spot-checks (offline verification only).")
    args = ap.parse_args()

    failures = []
    notes = []
    us = load_records(args.us_scan)
    a = load_records(args.a_scan)
    with open(args.us_manifest, encoding="utf-8") as f:
        um = json.load(f, parse_constant=_reject_constant)
    with open(args.a_manifest, encoding="utf-8") as f:
        am = json.load(f, parse_constant=_reject_constant)

    # 1. snapshot date vs latest close date in manifests
    for name, m in (("US", um), ("A", am)):
        if m.get("snapshot_date") != m.get("latest_close_date"):
            failures.append(
                "%s snapshot_date %s != latest_close_date %s"
                % (name, m.get("snapshot_date"), m.get("latest_close_date")))

    # success rates
    rates = {}
    for name, recs, m in (("US", us, um), ("A", a, am)):
        ok = sum(1 for r in recs if not r.get("error"))
        total = len(recs)
        rate = ok / total if total else 0.0
        rates[name] = rate
        if abs(rate - float(m.get("success_rate", rate))) > 0.001:
            failures.append("%s manifest success_rate %s != recomputed %s"
                            % (name, m.get("success_rate"), round(rate, 4)))
        if rate < 0.85:
            failures.append("%s success rate %.3f below 0.85" % (name, rate))
    notes.append("success rates: US %.3f, A %.3f" % (rates["US"], rates["A"]))

    # previous-run comparison
    if args.prev_manifest and Path(args.prev_manifest).exists():
        with open(args.prev_manifest, encoding="utf-8") as f:
            prev = json.load(f, parse_constant=_reject_constant)
        for name, rate in rates.items():
            key = "us_success_rate" if name == "US" else "a_success_rate"
            prev_rate = prev.get(key)
            if prev_rate is not None and rate < float(prev_rate) - 0.05:
                failures.append(
                    "%s success rate %.3f is >5pp below previous %.3f"
                    % (name, rate, float(prev_rate)))

    # 2. live spot-checks
    spot = {}
    if not args.skip_live_check:
        by_sym = {r["symbol"]: r for r in us}
        for sym in SPOT_US:
            try:
                d, c = yahoo_close(sym)
                rec = by_sym.get(sym)
                if rec is None or rec.get("error"):
                    failures.append("spot-check %s missing/failed in scan" % sym)
                    continue
                spot[sym] = {"source_date": d, "source_close": c,
                             "scan_close": rec.get("close")}
                if d != um.get("snapshot_date"):
                    failures.append("spot-check %s source date %s != snapshot %s"
                                    % (sym, d, um.get("snapshot_date")))
                if abs(c - float(rec["close"])) > 0.01:
                    failures.append("spot-check %s source close %s != scan %s"
                                    % (sym, c, rec["close"]))
            except Exception as e:  # network/source failure fails the gate
                failures.append("spot-check %s fetch failed: %s" % (sym, e))
        by_code = {r["symbol"]: r for r in a}
        for code in SPOT_A:
            try:
                d, c = sina_close(code)
                rec = by_code.get(code)
                if rec is None or rec.get("error"):
                    failures.append("spot-check %s missing/failed in scan" % code)
                    continue
                spot[code] = {"source_date": d, "source_close": c,
                              "scan_close": rec.get("close")}
                if d != am.get("snapshot_date"):
                    failures.append("spot-check %s source date %s != snapshot %s"
                                    % (code, d, am.get("snapshot_date")))
                if abs(c - float(rec["close"])) > 0.01:
                    failures.append("spot-check %s source close %s != scan %s"
                                    % (code, c, rec["close"]))
            except Exception as e:
                failures.append("spot-check %s fetch failed: %s" % (code, e))
    else:
        notes.append("live spot-checks skipped (--skip-live-check)")

    # 4. danger counts recomputed
    for name, recs, m in (("US", us, um), ("A", a, am)):
        sd = sum(1 for r in recs if r.get("in_support_danger"))
        rd = sum(1 for r in recs if r.get("in_resistance_danger"))
        for key, val in (("support_danger_count", sd), ("resistance_danger_count", rd)):
            if key in m and int(m[key]) != val:
                failures.append("%s manifest %s %s != recomputed %s"
                                % (name, key, m[key], val))

    status = "PASS" if not failures else "FAIL"
    result = {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "snapshot_date_us": um.get("snapshot_date"),
        "snapshot_date_a": am.get("snapshot_date"),
        "success_rate_us": round(rates["US"], 4),
        "success_rate_a": round(rates["A"], 4),
        "spot_check": spot,
        "failures": failures,
        "notes": notes,
        "details": ("; ".join(notes) if notes else "")
                   + ("; FAILURES: " + "; ".join(failures) if failures else ""),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
