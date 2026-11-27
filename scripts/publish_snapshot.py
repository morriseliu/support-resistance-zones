#!/usr/bin/env python3
"""Publish a validated snapshot into docs/data/ (GitHub Pages source).

Only ever run after validate_snapshot.py exits 0. Copies the fresh scan
JSON/CSV + kline files under stable names and writes the combined
docs/data/manifest.json that the site banner reads (snapshot dates,
generated_at, per-market counts, validation status). Old files are
replaced only here, so a failed gate can never publish stale data as new.

Scan JSONs are annotated with "is_new_danger" before writing: a row in
danger today (either side) that was in neither danger list in the
previously published snapshot, and only when the benchmark date advanced
(same-day republication never marks anything new, so a re-scan of the
same day cannot manufacture "new" entries). The previously published
scan file is read before it is overwritten.
"""
import argparse
import glob
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def normalize_kline(src_path, dst_path):
    """Normalize a per-ticker kline file to the site format:
    {date:[], open:[], high:[], low:[], close:[], volume:[]} (last 250 bars).
    Accepts a bare list of bar dicts, a {bars:[...]} dict, or arrays."""
    with open(src_path, encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, list):
        bars = d
    elif isinstance(d, dict) and isinstance(d.get("bars"), list):
        bars = d["bars"]
    elif isinstance(d, dict) and isinstance(d.get("date"), list):
        shutil.copyfile(src_path, dst_path)
        return
    else:
        raise SystemExit("unrecognized kline format: %s" % src_path)
    out = {k: [] for k in ("date", "open", "high", "low", "close", "volume")}
    for b in bars:
        out["date"].append(str(b["date"])[:10])
        for k in ("open", "high", "low", "close"):
            out[k].append(round(float(b[k]), 2))
        out["volume"].append(float(b.get("volume") or 0))
    with open(dst_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)


def danger_symbols(rows):
    """Symbols in either danger list (support or resistance)."""
    out = set()
    for r in rows:
        if r.get("in_support_danger") or r.get("in_resistance_danger"):
            out.add(r.get("symbol"))
    return out


def load_rows(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def annotate_new_danger(new_scan_path, prev_scan_path, out_path):
    """Write the new scan JSON with an "is_new_danger" flag per row.

    True only when the row is in danger today, was in neither danger list
    in the previously published snapshot, and the benchmark date advanced
    past the previous snapshot's date. A missing/unreadable previous file
    (first publish) or a same/earlier benchmark date marks nothing new.
    """
    with open(new_scan_path, encoding="utf-8") as f:
        rows = json.load(f)
    new_date = rows[0].get("benchmark_date") if rows else None
    prev_rows = load_rows(prev_scan_path) if Path(prev_scan_path).exists() else []
    prev_date = prev_rows[0].get("benchmark_date") if prev_rows else None
    advanced = bool(prev_rows) and bool(new_date) and bool(prev_date) and new_date > prev_date
    prev_danger = danger_symbols(prev_rows) if advanced else set()
    n_new = 0
    for r in rows:
        is_new = bool(advanced) and bool(
            r.get("in_support_danger") or r.get("in_resistance_danger")
        ) and r.get("symbol") not in prev_danger
        r["is_new_danger"] = is_new
        n_new += int(is_new)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return n_new


def one(pattern):
    hits = sorted(glob.glob(pattern))
    if not hits:
        raise SystemExit("no file matches %s" % pattern)
    return Path(hits[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-dir", required=True, help="refresh_out/")
    ap.add_argument("--data-dir", required=True, help="docs/data/")
    ap.add_argument("--validation", required=True)
    args = ap.parse_args()

    scan = Path(args.scan_dir)
    data = Path(args.data_dir)
    (data / "kline" / "us").mkdir(parents=True, exist_ok=True)
    (data / "kline" / "a").mkdir(parents=True, exist_ok=True)

    with open(args.validation, encoding="utf-8") as f:
        val = json.load(f)
    if val.get("status") != "PASS":
        raise SystemExit("refusing to publish: validation status is %s" % val.get("status"))

    us_scan = one(str(scan / "us" / "us_scan_*.json"))
    a_scan = one(str(scan / "a" / "a_scan_*.json"))
    with open(scan / "us" / "us_manifest.json", encoding="utf-8") as f:
        um = json.load(f)
    with open(scan / "a" / "a_manifest.json", encoding="utf-8") as f:
        am = json.load(f)

    n_new_us = annotate_new_danger(us_scan, data / "us_scan.json", data / "us_scan.json")
    n_new_a = annotate_new_danger(a_scan, data / "a_scan.json", data / "a_scan.json")
    for src in glob.glob(str(scan / "us" / "us_scan_*.csv")):
        shutil.copyfile(src, data / "us_scan.csv")
    for src in glob.glob(str(scan / "a" / "a_scan_*.csv")):
        shutil.copyfile(src, data / "a_scan.csv")
    for src in glob.glob(str(scan / "kline" / "us" / "*.json")):
        normalize_kline(src, data / "kline" / "us" / Path(src).name)
    for src in glob.glob(str(scan / "kline" / "a" / "*.json")):
        normalize_kline(src, data / "kline" / "a" / Path(src).name)

    manifest = {
        "snapshot_date_us": um["snapshot_date"],
        "snapshot_date_a": am["snapshot_date"],
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "us_universe": um.get("universe_count"),
        "a_universe": am.get("universe_count"),
        "us_success": um.get("success"),
        "a_success": am.get("success"),
        "us_success_rate": val.get("success_rate_us"),
        "a_success_rate": val.get("success_rate_a"),
        "validation": {
            "status": val["status"],
            "checked_at": val["checked_at"],
            "details": val.get("details", ""),
            "failures": val.get("failures", []),
        },
    }
    with open(data / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("published snapshot US %s / A %s (new danger: US %d, A %d)" % (um["snapshot_date"], am["snapshot_date"], n_new_us, n_new_a))


if __name__ == "__main__":
    main()
