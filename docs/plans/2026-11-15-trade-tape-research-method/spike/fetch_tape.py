"""Spike (plan step 1): fetch the full public Bitfinex funding trade tape.

Read-only. Writes one CSV per symbol into OUT_DIR (never into the repo or a DB).
Resumable: restarts from the last MTS already on disk; duplicate trade ids at
page boundaries are dropped. Stdlib only.

Usage:
    python3 fetch_tape.py OUT_DIR [--symbols fUST,fUSD] [--start 2022-01-01]
    python3 fetch_tape.py OUT_DIR --summarize   # print the measurement JSON

Output row: id,mts,amount,rate,period  (amount keeps its sign: negative = the
borrower was the taker).
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

BASE = "https://api-pub.bitfinex.com/v2/trades/{sym}/hist"
UA = "Mozilla/5.0 (research spike)"
LIMIT = 10000
PACE_S = 15.0  # 2.2 s drew a 429 every ~4 requests on the full backfill (2026-11-15)
STOP_ON_429 = False
BUCKETS = [(2, 2), (3, 6), (7, 14), (15, 29), (30, 30), (31, 120)]


def bucket(period: int) -> str:
    for lo, hi in BUCKETS:
        if lo <= period <= hi:
            return f"p{lo}" if lo == hi else f"p{lo}-{hi}"
    return f"p{period}"


def get(sym: str, start: int, end: int, stats: dict) -> list[list]:
    url = f"{BASE.format(sym=sym)}?start={start}&end={end}&limit={LIMIT}&sort=1"
    while True:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                stats["requests"] += 1
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                stats["http_429"] += 1
                if STOP_ON_429:  # Bitfinex blocks the whole IP for 60 s; on a shared host stop
                    raise SystemExit(3) from None
                time.sleep(60)
                continue
            if exc.code >= 500:
                stats["http_5xx"] += 1
                time.sleep(15)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            stats["net_errors"] += 1
            time.sleep(15)


def last_row(path: Path) -> tuple[int, set[int]]:
    """Return (last mts, ids at that mts) so a restart resumes without dupes."""
    if not path.exists() or path.stat().st_size == 0:
        return 0, set()
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 2_000_000))
        tail = fh.read().decode().splitlines()[1:]
    rows = [r.split(",") for r in tail if r and not r.startswith("id")]
    if not rows:  # header only: an earlier run stopped before its first page
        return 0, set()
    last_mts = int(rows[-1][1])
    return last_mts, {int(r[0]) for r in rows if int(r[1]) == last_mts}


def fetch(sym: str, out: Path, start_ms: int) -> dict:
    path = out / f"{sym}.csv"
    last_mts, seen = last_row(path)
    cursor = max(start_ms, last_mts)
    end_ms = int(time.time() * 1000)
    stats = {"requests": 0, "http_429": 0, "http_5xx": 0, "net_errors": 0, "rows": 0}
    t0 = time.monotonic()
    new_file = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["id", "mts", "amount", "rate", "period"])
        while cursor < end_ms:
            page = get(sym, cursor, end_ms, stats)
            fresh = [r for r in page if r[0] not in seen]
            if not fresh:
                if len(page) >= LIMIT:  # >LIMIT trades in one ms: cannot page past
                    raise SystemExit(f"{sym}: stuck at mts={cursor}")
                break
            for r in fresh:
                w.writerow([r[0], r[1], repr(r[2]), repr(r[3]), r[4]])
            stats["rows"] += len(fresh)
            new_cursor = page[-1][1]
            seen = {r[0] for r in page if r[1] == new_cursor} | (
                seen if new_cursor == cursor else set()
            )
            cursor = new_cursor
            if len(page) < LIMIT:
                break
            if stats["requests"] % 50 == 0:
                fh.flush()
                day = datetime.fromtimestamp(cursor / 1000, UTC).date()
                print(
                    f"{sym} {day} rows+={stats['rows']} req={stats['requests']} 429={stats['http_429']}",
                    flush=True,
                )
            time.sleep(PACE_S)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return stats


def _q(hist: Counter[int], p: float) -> int:
    """Quantile of a value histogram (keeps memory flat on 100M-row tapes)."""
    target, acc = p * sum(hist.values()), 0
    for v in sorted(hist):
        acc += hist[v]
        if acc >= target:
            return v
    return max(hist)


def _iso(ms: int) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms else None


def summarize(out: Path) -> dict:
    """One streaming pass per symbol; memory does not grow with row count.

    Duplicates can only arise at page boundaries (same MTS), so they are checked
    within one MTS. The global-id check uses the 1st of every month as sample days.
    """
    result: dict = {}
    sample_ids: dict[str, dict[str, list[int]]] = {}
    for path in sorted(out.glob("f*.csv")):
        sym = path.stem
        daily: Counter[str] = Counter()
        yearly: Counter[str] = Counter()
        buckets: Counter[str] = Counter()
        neg_by_bucket: Counter[str] = Counter()
        p30_daily: Counter[str] = Counter()
        gap_hist: Counter[int] = Counter()  # whole seconds
        neg = pos = rows = dupes = inversions = 0
        prev_mts = prev_id = prev_p30 = None
        max_gap = (0, 0)
        max_p30_gap = (0, 0)
        mts_ids: set[int] = set()
        samples: dict[str, list[int]] = {}
        with path.open() as fh:
            next(fh)
            for line in fh:
                f = line.rstrip("\n").split(",")
                tid, mts, amt, per = int(f[0]), int(f[1]), float(f[2]), int(f[4])
                if mts != prev_mts:
                    mts_ids = set()
                elif tid in mts_ids:
                    dupes += 1
                    continue
                mts_ids.add(tid)
                rows += 1
                day = datetime.fromtimestamp(mts / 1000, UTC).strftime("%Y-%m-%d")
                daily[day] += 1
                yearly[day[:4]] += 1
                if day.endswith("-01"):
                    samples.setdefault(day, []).append(tid)
                b = bucket(per)
                buckets[b] += 1
                if per == 30:
                    p30_daily[day] += 1
                    if prev_p30 is not None and mts - prev_p30 > max_p30_gap[0]:
                        max_p30_gap = (mts - prev_p30, prev_p30)
                    prev_p30 = mts
                if amt < 0:
                    neg += 1
                    neg_by_bucket[b] += 1
                else:
                    pos += 1
                if prev_mts is not None:
                    g = mts - prev_mts
                    gap_hist[g // 1000] += 1
                    if g > max_gap[0]:
                        max_gap = (g, prev_mts)
                if prev_id is not None and tid < prev_id:
                    inversions += 1
                prev_mts, prev_id = mts, tid
        sample_ids[sym] = samples
        counts = list(daily.values())
        days = sorted(daily)
        first = datetime.fromisoformat(days[0]).replace(tzinfo=UTC)
        span = (datetime.fromisoformat(days[-1]).replace(tzinfo=UTC) - first).days + 1
        all_days = [
            datetime.fromtimestamp(first.timestamp() + 86400 * i, UTC).strftime("%Y-%m-%d")
            for i in range(span)
        ]
        p30 = sorted(p30_daily.get(d, 0) for d in all_days)
        result[sym] = {
            "rows": rows,
            "duplicate_ids_dropped": dupes,
            "first_day": days[0],
            "last_day": days[-1],
            "days_with_zero_trades": span - len(days),
            "rows_by_year": dict(sorted(yearly.items())),
            "daily_rows": {
                "min": min(counts),
                "median": statistics.median(counts),
                "max": max(counts),
            },
            "rows_by_bucket": dict(sorted(buckets.items())),
            "negative_share": round(neg / (neg + pos), 4),
            "negative_share_by_bucket": {
                k: round(neg_by_bucket[k] / buckets[k], 4) for k in sorted(buckets)
            },
            "p30_daily": {
                "min": p30[0],
                "p10": p30[len(p30) // 10],
                "median": statistics.median(p30),
                "max": p30[-1],
                "zero_days": p30.count(0),
            },
            "p30_longest_gap": {
                "hours": round(max_p30_gap[0] / 3.6e6, 2),
                "starts": _iso(max_p30_gap[1]),
            },
            "gap_seconds": {
                "p50": _q(gap_hist, 0.5),
                "p99": _q(gap_hist, 0.99),
                "p999": _q(gap_hist, 0.999),
                "max": max(gap_hist),
            }
            if gap_hist
            else None,
            "longest_gap": {"hours": round(max_gap[0] / 3.6e6, 2), "starts": _iso(max_gap[1])},
            "id_inversions_vs_time_order": inversions,
            "csv_bytes": path.stat().st_size,
        }
    if len(sample_ids) == 2:
        (a, sa), (b, sb) = sample_ids.items()
        per_day = []
        for day in sorted(set(sa) & set(sb)):
            ia, ib = set(sa[day]), set(sb[day])
            merged = sorted([(x, 0) for x in ia] + [(x, 1) for x in ib])
            switches = sum(1 for p, n in pairwise(merged) if p[1] != n[1])
            per_day.append((day, len(ia & ib), len(ia), len(ib), switches))
        result["global_id_check"] = {
            "sample_days": len(per_day),
            "shared_ids_total": sum(d[1] for d in per_day),
            "min_interleave_switches": min(d[4] for d in per_day),
            "days": {d[0]: {"shared": d[1], a: d[2], b: d[3], "switches": d[4]} for d in per_day},
        }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--symbols", default="fUST,fUSD")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--pace", type=float, default=PACE_S, help="seconds between requests")
    ap.add_argument("--stop-on-429", action="store_true", help="exit 3 on the first rate limit")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.summarize:
        json.dump(summarize(args.out), sys.stdout, indent=2)
        print()
        return
    globals().update(PACE_S=args.pace, STOP_ON_429=args.stop_on_429)
    start_ms = int(datetime.fromisoformat(args.start).replace(tzinfo=UTC).timestamp() * 1000)
    for sym in args.symbols.split(","):
        stats = fetch(sym, args.out, start_ms)
        print(json.dumps({"symbol": sym, **stats}), flush=True)
        with (args.out / "fetch_runs.jsonl").open("a") as fh:
            fh.write(
                json.dumps({"symbol": sym, "finished": datetime.now(UTC).isoformat(), **stats})
                + "\n"
            )


if __name__ == "__main__":
    main()
