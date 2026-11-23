"""Support/resistance zone engine — frozen spec v1.0 (2026-11-22).

Rules (SPEC_FROZEN_20261122.md, do not change parameters):
- Swing point: bar whose high/low is max/min of itself +/- 5 bars (confirmed only
  when 5 following bars exist).
- Zone: group of swing lows (support) or swing highs (resistance); zone = [min, max]
  of the group's prices; width must be <= 1.0 * ATR(14) (max ATR among touches).
- A zone needs >= 2 touches, consecutive kept touches >= 5 trading days apart.
  Single-touch points are not zones.
- Break: judged by CLOSE vs zone edge only. Support breaks if close < zone low;
  resistance breaks if close > zone high. Between first and last touch, any break
  invalidates the grouping. Price-similar swings separated by a break are split
  at the break and counted separately (chronological split on running edge).
- Distance (sec.4): close inside zone -> 0. Support above: (close-hi)/hi.
  Resistance below: (lo-close)/lo. <=3% = near. Close beyond edge = crossed.
- Selection: nearest 3 support / 3 resistance to the benchmark close.
  A segment broken after its last touch is excluded if the benchmark close has
  returned to the other side of the zone within 0.5% of the edge (void, needs
  re-counting); if the benchmark close is beyond the edge by >0.5% it is listed
  on the opposite side flagged flipped. A segment broken after but with close
  within 0.5% of the edge stays in its original list flagged crossed-edge.
- Lookback: last 500 daily bars. Weekly bars are computed independently by the
  caller (weekly OHLC) and run through the same functions.
"""
import numpy as np
import pandas as pd

SWING = 5
LOOKBACK = 500
NEAR_PCT = 0.03
FLIP_PCT = 0.005


def add_atr(df):
    df = df.sort_values("date").reset_index(drop=True).copy()
    prev_close = df["close"].shift()
    tr = np.maximum(df["high"] - df["low"],
                    np.maximum(abs(df["high"] - prev_close), abs(df["low"] - prev_close)))
    df["atr"] = tr.rolling(14).mean()
    return df


def swing_points(df):
    """Return DataFrame: dir (S/R), price, date, atr, idx (position in df)."""
    rows = []
    h = df["high"].values
    l = df["low"].values
    for i in range(SWING, len(df) - SWING):
        if h[i] == max(h[i - SWING:i + SWING + 1]):
            rows.append(("R", float(h[i]), df["date"].iloc[i], float(df["atr"].iloc[i]), i))
        if l[i] == min(l[i - SWING:i + SWING + 1]):
            rows.append(("S", float(l[i]), df["date"].iloc[i], float(df["atr"].iloc[i]), i))
    return pd.DataFrame(rows, columns=["dir", "price", "date", "atr", "idx"])


def _dedup_touches(touches):
    """Keep touches >=5 trading days apart (by index position in window)."""
    kept = []
    for t in touches:
        if not kept or (t["idx"] - kept[-1]["idx"]) >= 5:
            kept.append(t)
    return kept



def _validate_piece(df, touches, direction):
    """Validate one candidate segment (touches already >=2, deduped).
    Returns [zone] or []."""
    touches = _dedup_touches(touches)
    if len(touches) < 2:
        return []
    lo = min(t["price"] for t in touches)
    hi = max(t["price"] for t in touches)
    width = hi - lo
    atr_max = max(t["atr"] for t in touches)
    atr_last = max(touches, key=lambda t: t["date"])["atr"]
    if width > atr_last:
        srt = sorted(touches, key=lambda t: t["price"])
        gaps = [srt[i + 1]["price"] - srt[i]["price"] for i in range(len(srt) - 1)]
        k = gaps.index(max(gaps))
        out = []
        for piece in (srt[:k + 1], srt[k + 1:]):
            out.extend(_validate_piece(df, piece, direction))
        return out
    first, last = touches[0]["date"], touches[-1]["date"]
    between = df[(df["date"] > first) & (df["date"] < last)]
    after = df[df["date"] > last]
    if direction == "S":
        n_between = int((between["close"] < lo).sum())
        worst_between = float(between["close"].min()) if len(between) else None
        n_after = int((after["close"] < lo).sum())
        worst_after = float(after["close"].min()) if len(after) else None
    else:
        n_between = int((between["close"] > hi).sum())
        worst_between = float(between["close"].max()) if len(between) else None
        n_after = int((after["close"] > hi).sum())
        worst_after = float(after["close"].max()) if len(after) else None
    if n_between > 0:
        return []
    return [{
        "dir": direction, "lo": round(lo, 4), "hi": round(hi, 4),
        "width": round(width, 4), "atr_max": round(atr_max, 4),
        "touches": [{"date": str(t["date"].date()), "price": round(t["price"], 4)}
                    for t in touches],
        "n_touches": len(touches),
        "first_touch": str(first.date()), "last_touch": str(last.date()),
        "breaks_between": n_between, "worst_between": worst_between,
        "breaks_after": n_after, "worst_after": worst_after,
    }]

def _segments_for_direction(df, pts, direction):
    """Group by price (greedy, sorted by price, gap rule vs ATR), then split
    chronologically at breaks against the running zone edge."""
    pts = pts.sort_values("price").reset_index(drop=True)
    groups = []
    cur = [pts.iloc[0]]
    for i in range(1, len(pts)):
        row = pts.iloc[i]
        gmin = min(x["price"] for x in cur)
        if row["price"] - gmin <= row["atr"]:
            cur.append(row)
        else:
            groups.append(cur)
            cur = [row]
    groups.append(cur)

    segments = []
    for g in groups:
        chrono = sorted(g, key=lambda x: x["date"])
        parts = [[chrono[0]]]
        for row in chrono[1:]:
            curp = parts[-1]
            # Prospective edge including the new touch: breaks are judged
            # against the FINAL zone edge, not the running edge (TSLA
            # Sep 2026: closes dipped below the first touch 354.05 but
            # stayed above the final zone low 345.88, so no break).
            plo = min(min(x["price"] for x in curp), row["price"])
            phi = max(max(x["price"] for x in curp), row["price"])
            between = df[(df["date"] > curp[-1]["date"]) & (df["date"] < row["date"])]
            if direction == "S":
                broken = bool((between["close"] < plo).any())
            else:
                broken = bool((between["close"] > phi).any())
            if broken:
                parts.append([row])
            else:
                curp.append(row)
        for part in parts:
            touches = _dedup_touches(part)
            if len(touches) < 2:
                continue
            lo = min(t["price"] for t in touches)
            hi = max(t["price"] for t in touches)
            width = hi - lo
            atr_max = max(t["atr"] for t in touches)
            atr_last = max(touches, key=lambda t: t["date"])["atr"]
            if width > atr_last:
                # Split at the largest sorted-price gap and re-validate
                # each piece (META 520-540: width 19.92 > ATR 19.79 at the
                # last touch -> [520.26,524.49] + [537.27,540.18]).
                srt = sorted(touches, key=lambda t: t["price"])
                gaps = [srt[i + 1]["price"] - srt[i]["price"] for i in range(len(srt) - 1)]
                k = gaps.index(max(gaps))
                for piece in (srt[:k + 1], srt[k + 1:]):
                    if len(piece) >= 2:
                        segments.extend(_validate_piece(df, piece, direction))
                continue
            first, last = touches[0]["date"], touches[-1]["date"]
            between = df[(df["date"] > first) & (df["date"] < last)]
            after = df[df["date"] > last]
            if direction == "S":
                n_between = int((between["close"] < lo).sum())
                worst_between = float(between["close"].min()) if len(between) else None
                n_after = int((after["close"] < lo).sum())
                worst_after = float(after["close"].min()) if len(after) else None
            else:
                n_between = int((between["close"] > hi).sum())
                worst_between = float(between["close"].max()) if len(between) else None
                n_after = int((after["close"] > hi).sum())
                worst_after = float(after["close"].max()) if len(after) else None
            if n_between > 0:
                continue
            segments.append({
                "dir": direction, "lo": round(lo, 4), "hi": round(hi, 4),
                "width": round(width, 4), "atr_max": round(atr_max, 4),
                "touches": [{"date": str(t["date"].date()), "price": round(t["price"], 4)}
                            for t in touches],
                "n_touches": len(touches),
                "first_touch": str(first.date()), "last_touch": str(last.date()),
                "breaks_between": n_between, "worst_between": worst_between,
                "breaks_after": n_after, "worst_after": worst_after,
            })
    return segments


def compute_zones(df):
    """df: daily (or weekly) OHLCV with date/open/high/low/close/volume.
    Returns list of zone dicts (all valid segments, before nearest selection)."""
    d = add_atr(df)
    d = d.tail(LOOKBACK).reset_index(drop=True)
    sw = swing_points(d)
    zones = []
    for direction in ("S", "R"):
        pts = sw[sw["dir"] == direction]
        if len(pts):
            zones.extend(_segments_for_direction(d, pts, direction))
    return zones, float(d["close"].iloc[-1]), d


def distance_to_zone(close, zone):
    if zone["lo"] <= close <= zone["hi"]:
        return 0.0, "inside"
    if zone["dir"] == "S":
        if close > zone["hi"]:
            return (close - zone["hi"]) / zone["hi"], "above"
        return (zone["lo"] - close) / zone["lo"], "crossed_below"
    else:
        if close < zone["lo"]:
            return (zone["lo"] - close) / zone["lo"], "below"
        return (close - zone["hi"]) / zone["hi"], "crossed_above"


def select_zones(zones, close):
    """Nearest 3 INTACT support / 3 INTACT resistance, plus flipped zones
    listed separately (spec sec.6: flipped are labelled separately, not
    mixed with unbroken zones).

    Classification by breaks_after (closes beyond the edge after the last
    touch) and the benchmark close position:
    - beyond edge by >0.5% in the break direction -> flipped (opposite side)
    - beyond edge by <=0.5% -> stays on its original side, flagged crossed_edge
    - breaks_after>0 but close back on/inside the original side -> VOID
      (zone broken then returned: needs re-counting, e.g. TSLA 384.04-388.56
      and 338.00-348.98). breaks_after==0 -> intact.
    """
    supports, resistances = [], []
    flip_supports, flip_resistances = [], []
    for z in zones:
        z = dict(z)
        dist, pos = distance_to_zone(close, z)
        z["distance"] = round(dist, 6)
        z["position"] = pos
        z["flipped"] = False
        z["crossed_edge"] = False
        ba = z["breaks_after"]
        if z["dir"] == "S":
            if pos == "above" or pos == "inside":
                if ba > 0 and close > z["hi"]:
                    continue  # void: broken, price recovered above
                supports.append(z)
            elif pos == "crossed_below":
                beyond = (z["lo"] - close) / z["lo"]
                if beyond > FLIP_PCT:
                    z["flipped"] = True
                    flip_resistances.append(z)
                else:
                    z["crossed_edge"] = True
                    supports.append(z)
        else:  # R
            if pos == "below" or pos == "inside":
                if ba > 0 and close < z["lo"]:
                    continue  # void: broken, price returned below
                resistances.append(z)
            elif pos == "crossed_above":
                beyond = (close - z["hi"]) / z["hi"]
                if beyond > FLIP_PCT:
                    z["flipped"] = True
                    flip_supports.append(z)
                else:
                    z["crossed_edge"] = True
                    resistances.append(z)
    # Void check for inside-position zones with breaks_after>0: a zone
    # broken after its last touch whose close is back inside it is void.
    def _void_inside(z):
        return z["breaks_after"] > 0 and z["position"] == "inside"
    supports = [z for z in supports if not _void_inside(z)]
    resistances = [z for z in resistances if not _void_inside(z)]
    for lst in (supports, resistances, flip_supports, flip_resistances):
        lst.sort(key=lambda z: z["distance"])
    return supports[:3], resistances[:3], flip_supports[:3], flip_resistances[:3]


def weekly_bars(df):
    d = df.sort_values("date").copy()
    d["date"] = pd.to_datetime(d["date"])
    d["wk"] = d["date"].dt.to_period("W").apply(lambda r: r.start_time)
    w = d.groupby("wk").agg(date=("date", "max"), open=("open", "first"),
                            high=("high", "max"), low=("low", "min"),
                            close=("close", "last"), volume=("volume", "sum")).reset_index(drop=True)
    return w


def weekly_confluence(daily_zones, weekly_swings):
    """Mark a daily zone 'weekly_same_price' if a WEEKLY SWING POINT of the
    same direction has its price inside the daily zone interval (spec sec.6:
    '周线同价' = weekly swing at the same price, e.g. TSM weekly low 313.80)."""
    out = []
    for z in daily_zones:
        flag = False
        for _, w in weekly_swings.iterrows():
            if w["dir"] != z["dir"]:
                continue
            if z["lo"] - 0.001 <= round(float(w["price"]), 4) <= z["hi"] + 0.001:
                flag = True
                break
        z = dict(z)
        z["weekly_same_price"] = flag
        out.append(z)
    return out


def analyze(df):
    """Full analysis for one ticker. Returns dict with close, supports,
    resistances (nearest 3 each, weekly flags applied), all zones."""
    zones, close, d = compute_zones(df)
    wb = weekly_bars(df)
    wsw = swing_points(add_atr(wb))
    wzones, _, _ = compute_zones(wb)
    zones = weekly_confluence(zones, wsw)
    supports, resistances, fs, fr = select_zones(zones, close)
    return {"close": close, "supports": supports, "resistances": resistances,
            "flipped_supports": fs, "flipped_resistances": fr,
            "all_zones": zones, "weekly_zones": wzones,
            "benchmark_date": str(pd.to_datetime(d["date"].iloc[-1]).date())}
