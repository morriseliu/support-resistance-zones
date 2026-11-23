#!/usr/bin/env python3
"""Publish a validated snapshot into docs/data/ (GitHub Pages source).

Only ever run after validate_snapshot.py exits 0. Copies the fresh scan
JSON/CSV + kline files under stable names and writes the combined
docs/data/manifest.json that the site banner reads (snapshot dates,
generated_at, per-market counts, validation status). Old files are
replaced only here, so a failed gate can never publish stale data as new.
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

    shutil.copyfile(us_scan, data / "us_scan.json")
    shutil.copyfile(a_scan, data / "a_scan.json")
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
    print("published snapshot US %s / A %s" % (um["snapshot_date"], am["snapshot_date"]))


if __name__ == "__main__":
    main()
