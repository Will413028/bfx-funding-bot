"""Step 3: time one full-history replay with the real engine module (read-only).

One hypothetical offer per hour over the whole tape, period P, rate = the last
public trade rate at or before the hour (a market-like quote), 24 h wait; both
bounds. Prints wall seconds, offers, fill fractions and peak RSS.

    python replay_bench.py <tape-root> <symbol> <replay.py> <incomplete.py> [--period 2]

replay.py / incomplete.py are loaded from paths so the bench runs in an image
that predates them (step 3 is not released yet).
"""
from __future__ import annotations

import argparse
import importlib.util
import resource
import sys
import time
import types
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("tape_root", type=Path)
    p.add_argument("symbol")
    p.add_argument("replay_py", type=Path)
    p.add_argument("incomplete_py", type=Path)
    p.add_argument("--period", type=int, default=2)
    p.add_argument("--wait-h", type=int, default=24)
    a = p.parse_args()
    _load("bfx_funding_bot.modules.backtest.incomplete", a.incomplete_py)
    replay = _load("bfx_funding_bot.modules.backtest.replay", a.replay_py)

    days = sorted(f.name.removesuffix(".csv.gz") for f in (a.tape_root / a.symbol).glob("*.csv.gz"))
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    tape = replay.TapeDays(a.tape_root, a.symbol)
    quote_tape = replay.TapeDays(a.tape_root, a.symbol, cache_days=1)
    start = int(datetime.combine(first, datetime.min.time(), UTC).timestamp() * 1000)
    end = int(datetime.combine(last - timedelta(days=1), datetime.min.time(), UTC).timestamp() * 1000)
    hour, wait = 3_600_000, a.wait_h * 3_600_000
    offers = cons = opt = 0.0
    last_rate = None
    began = time.perf_counter()
    t0 = start + hour
    while t0 <= end:
        prior = quote_tape.window(t0 - hour, t0)
        if len(prior):
            last_rate = float(prior[-1, 3])
        if last_rate is not None:
            r = replay.replay_offer(
                replay.HypotheticalOffer(rate=Decimal(repr(last_rate)), period_days=a.period,
                                         placed_mts=t0, amount=Decimal("1000")),
                tape, max_wait_ms=wait)
            offers += 1
            cons += float(r.conservative.filled) / 1000
            opt += float(r.optimistic.filled) / 1000
        t0 += hour
    secs = time.perf_counter() - began
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"symbol={a.symbol} period={a.period} days={len(days)} offers={int(offers)} "
          f"seconds={secs:.1f} mean_fill_conservative={cons / offers:.3f} "
          f"mean_fill_optimistic={opt / offers:.3f} max_rss_mb={rss_mb:.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
