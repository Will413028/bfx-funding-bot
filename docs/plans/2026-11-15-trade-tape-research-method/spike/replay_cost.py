"""Step 1b items 2 and 3: market depth per period and replay cost with numpy.

Read-only; reads the tape CSV, writes only an .npy cache next to --cache.
Usage: python replay_cost.py <tape csv> --cache <dir> [--offers 1000] [--horizon-days 7]
Prints one JSON object.
"""

import argparse
import json
import resource
import time
from pathlib import Path

import numpy as np

DAY_MS = 86_400_000
BUCKETS = [(2, 2), (3, 6), (7, 14), (15, 29), (30, 30), (31, 120)]


def load_csv(path: Path) -> np.ndarray:
    data = np.loadtxt(path, delimiter=",", skiprows=1, dtype=np.float64)
    return data  # id, mts, amount, rate, period


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tape", type=Path)
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--offers", type=int, default=1000)
    ap.add_argument("--horizon-days", type=int, default=7)
    a = ap.parse_args()
    out: dict = {"tape": a.tape.name}
    cache = a.cache / (a.tape.stem + ".npz")
    if not cache.exists():
        t = time.perf_counter()
        raw = load_csv(a.tape)
        out["csv_parse_s"] = round(time.perf_counter() - t, 1)
        t = time.perf_counter()
        order = np.lexsort((raw[:, 1], raw[:, 4]))  # by period, then mts
        raw = raw[order]
        np.savez(cache, mts=raw[:, 1].astype(np.int64), amount=raw[:, 2],
                 rate=raw[:, 3], period=raw[:, 4].astype(np.int16))
        out["sort_save_s"] = round(time.perf_counter() - t, 1)
        del raw
    t = time.perf_counter()
    z = np.load(cache)
    mts, amount, rate, period = z["mts"], z["amount"], z["rate"], z["period"]
    out["npz_load_s"] = round(time.perf_counter() - t, 2)
    out["rows"] = int(mts.size)
    out["array_bytes"] = int(mts.nbytes + amount.nbytes + rate.nbytes + period.nbytes)

    # item 2: daily borrower-taker (amount < 0) volume per period bucket
    neg = amount < 0
    day = mts // DAY_MS
    d0, d1 = int(day.min()), int(day.max())
    depth = {}
    for lo, hi in BUCKETS:
        m = neg & (period >= lo) & (period <= hi)
        vol = np.bincount(day[m] - d0, weights=-amount[m], minlength=d1 - d0 + 1)
        depth[f"p{lo}-{hi}"] = {"median": round(float(np.median(vol))),
                                "p10": round(float(np.percentile(vol, 10)))}
    out["daily_taker_volume"] = depth

    # item 3: replay N hypothetical offers. Per period value: slice sorted by mts.
    pvals, starts = np.unique(period, return_index=True)
    ends = np.append(starts[1:], period.size)
    slices = {int(p): (int(s), int(e)) for p, s, e in zip(pvals, starts, ends, strict=True)}
    rng = np.random.default_rng(0)
    t_lo, t_hi = int(mts.min()), int(mts.max()) - a.horizon_days * DAY_MS
    offers_p = rng.choice([2, 2, 2, 7, 30, 120], a.offers)
    t = time.perf_counter()
    filled = 0
    for p_off in offers_p:
        t0 = int(rng.integers(t_lo, t_hi))
        t1 = t0 + a.horizon_days * DAY_MS
        size = 500.0
        got = 0.0
        for p, (s, e) in slices.items():
            if p > p_off:
                continue
            ms = mts[s:e]
            i, j = np.searchsorted(ms, [t0, t1])
            am, rt = amount[s + i:s + j], rate[s + i:s + j]
            if am.size == 0:
                continue
            r_off = float(np.median(rt))
            got += float(-am[(am < 0) & (rt > r_off)].sum())
        filled += got >= size
    out["replay_offers"] = a.offers
    out["replay_s"] = round(time.perf_counter() - t, 2)
    out["replay_filled"] = int(filled)
    out["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
