"""Period-threshold research runner (2026-09-27) — read-only over the market tables.

Question: lend 2d by default and a longer tenor (7/14/30) only when the rate is
above a threshold? Scores threshold rules against always-2d under early-repayment
scenarios (`modules.backtest.period_threshold`). Research only: never touches
live config, envelopes or policies, and only SELECTs.

Two price sources:

  candles  — hourly `funding_candles` closes: 2d = p2, 30d = p30 (a real traded
             30d rate; None in hours without a 30d trade). 7d has no candle series,
             so a SYNTHETIC 7d = p2 * (1 + --synthetic-7d-premium) is also scored
             (an estimate: it isolates the value of locking, not a measured price).
  book     — hourly `funding_book_snapshots` (last snapshot per hour): best
             exact-period ask for 2/7/14/30; None when that period has no visible
             ask in the top-25 levels (no price reference under exact-period pricing).

Run from backend/ (env DATABASE_URL):
  uv run python -m scripts.run_period_threshold --symbol fUST \\
      --start 2018-12-22 --end 2026-07-19 --out /tmp/pt-fust.md
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.period_threshold import (
    ALWAYS_2D,
    RepaymentScenario,
    SimResult,
    ThresholdRule,
    hourly_grid,
    limit_fill_grid,
    monthly_deltas,
    premium_by_bucket,
    simulate,
)

SCENARIOS: tuple[RepaymentScenario, ...] = (
    RepaymentScenario("held 100%", 1.0),
    RepaymentScenario("held 50%", 0.5),
    RepaymentScenario("held 25%", 0.25),
    RepaymentScenario("refinance if r2<0.8x locked", 1.0, refinance_on_drop=True),
)
LEVELS: tuple[float, ...] = (0.0, 0.0002, 0.0003, 0.0004, 0.0005, 0.0007, 0.001)
PERCENTILES: tuple[float, ...] = (0.8, 0.9, 0.95)


def _ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


def rules_for(period: int) -> list[ThresholdRule]:
    rules = [
        ThresholdRule(
            name=f"{period}d if r{period}>={lv:.4f} ({lv * 36500:.1f}% APR)"
            if lv
            else f"{period}d whenever r{period}>=r2",
            long_period_days=period,
            min_long_rate=lv,
        )
        for lv in LEVELS
    ]
    rules += [
        ThresholdRule(
            name=f"{period}d if r2>=trailing-30d p{int(q * 100)}",
            long_period_days=period,
            r2_percentile=q,
        )
        for q in PERCENTILES
    ]
    return rules


def render_table(
    title: str,
    start_ms: int,
    r2: Sequence[float | None],
    rlong: Sequence[float | None],
    period: int,
    scenarios: Sequence[RepaymentScenario] = SCENARIOS,
) -> str:
    """Cells: full-window Δ net APR vs always-2d / median-month Δ (share of months won)."""
    base = {s.name: simulate(r2, rlong, ALWAYS_2D, s) for s in scenarios}
    lines = [
        f"### {title}",
        "",
        "Cells: full-window Δ net APR vs always-2d (pp) / median per-month Δ (pp) "
        "(share of months won); `long%` = share of hours in a long credit under held 100%.",
        "",
        "| rule | long% | " + " | ".join(s.name for s in scenarios) + " |",
        "|---|---" + "|---" * len(scenarios) + "|",
        "| always_2d net APR | 0% | "
        + " | ".join(f"{base[s.name].net_apr * 100:.2f}%" for s in scenarios)
        + " |",
    ]
    for rule in rules_for(period):
        cells = []
        long_share = 0.0
        for s in scenarios:
            res: SimResult = simulate(r2, rlong, rule, s)
            if s is scenarios[0]:
                long_share = res.long_capital_share
            m = monthly_deltas(start_ms, r2, rlong, rule, s)
            delta = (res.net_apr - base[s.name].net_apr) * 100
            cells.append(
                f"{delta:+.2f} / {m.median_delta * 100:+.2f} ({m.win_share * 100:.0f}%)"
            )
        lines.append(f"| {rule.name} | {long_share * 100:.0f}% | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render_premium(
    title: str,
    start_ms: int,
    r2: Sequence[float | None],
    rlong: Sequence[float | None],
    bucket: str,
) -> str:
    rows = premium_by_bucket(start_ms, r2, rlong, bucket=bucket)
    lines = [
        f"### {title}",
        "",
        f"| {bucket} | hours | long ref available | median premium over 2d | median r2 (APR) |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        prem = "-" if r.median_premium is None else f"{r.median_premium * 100:+.1f}%"
        r2s = "-" if r.median_r2 is None else f"{r.median_r2 * 36500:.1f}%"
        lines.append(
            f"| {r.bucket} | {r.n_hours} | {r.long_available_share * 100:.0f}% | {prem} | {r2s} |"
        )
    return "\n".join(lines) + "\n"


Points = list[tuple[int, float]]


async def _load_candles(
    symbol: str, agg: str, start_ms: int, end_ms: int
) -> tuple[Points, Points]:
    engine = make_engine(Settings())
    sf = make_session_factory(engine)
    try:
        async with sf() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT mts, close, high FROM funding_candles WHERE symbol=:s "
                        "AND timeframe='1h' AND period_agg=:a AND mts>=:b AND mts<:e "
                        "ORDER BY mts"
                    ),
                    {"s": symbol, "a": agg, "b": start_ms, "e": end_ms},
                )
            ).all()
    finally:
        await engine.dispose()
    good = [(int(m), float(c), float(h)) for m, c, h in rows if c is not None and c > 0]
    return [(m, c) for m, c, _ in good], [(m, h) for m, _, h in good]


async def _load_book(
    symbol: str, start_ms: int, end_ms: int, periods: Sequence[int]
) -> tuple[dict[int, Points], int]:
    engine = make_engine(Settings())
    sf = make_session_factory(engine)
    try:
        async with sf() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT captured_at_ms, payload FROM funding_book_snapshots "
                        "WHERE symbol=:s AND captured_at_ms>=:b AND captured_at_ms<:e "
                        "ORDER BY captured_at_ms"
                    ),
                    {"s": symbol, "b": start_ms, "e": end_ms},
                )
            ).all()
    finally:
        await engine.dispose()
    out: dict[int, Points] = {p: [] for p in periods}
    for ts, payload in rows:
        best: dict[int, float] = {}
        for rate, period, _count, _amount in payload.get("asks", []):
            p = int(period)
            if p in out and (p not in best or float(rate) < best[p]):
                best[p] = float(rate)
        for p, r in best.items():
            out[p].append((int(ts), r))
    return out, len(rows)


async def _amain(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--symbol", default="fUST")
    ap.add_argument("--source", choices=("candles", "book"), default="candles")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, exclusive)")
    ap.add_argument("--synthetic-7d-premium", type=float, default=0.048)
    ap.add_argument("--rate-cap", type=float, default=None, help="clip daily rates (no-spike view)")
    ap.add_argument("--premium-bucket", choices=("year", "month"), default="year")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    start_ms, end_ms = _ms(args.start), _ms(args.end)
    parts = [f"## {args.symbol} {args.source} {args.start} .. {args.end}\n"]

    if args.source == "candles":
        c2, h2 = await _load_candles(args.symbol, "p2", start_ms, end_ms)
        c30, h30 = await _load_candles(args.symbol, "p30", start_ms, end_ms)
        close2, close30 = hourly_grid(c2, start_ms, end_ms), hourly_grid(c30, start_ms, end_ms)
        parts.append(render_premium("30d (p30 close) premium over 2d (p2 close), same hour", start_ms, close2, close30, args.premium_bucket))
        # Executable grids: priced at the previous hour's close, filled only if the
        # tenor traded at/above it during the hour (see limit_fill_grid).
        r2 = limit_fill_grid(close2, hourly_grid(h2, start_ms, end_ms), args.rate_cap)
        r30 = limit_fill_grid(close30, hourly_grid(h30, start_ms, end_ms), args.rate_cap)
        parts.append(
            f"hours={len(r2)} r2 present={sum(x is not None for x in r2)} "
            f"r30 present={sum(x is not None for x in r30)} (executable hours; rate cap={args.rate_cap})\n"
        )
        parts.append(render_table("30d measured (p30 trades; None = no fillable 30d trade that hour)", start_ms, r2, r30, 30))
        prem = args.synthetic_7d_premium
        for label, p in ((f"+{prem * 100:.1f}%", prem), ("+0%", 0.0)):
            r7 = [None if x is None else x * (1 + p) for x in r2]
            parts.append(
                render_table(f"7d SYNTHETIC (estimate: r7 = r2 {label}, fillable whenever 2d is)", start_ms, r2, r7, 7)
            )
    else:
        series, n_snap = await _load_book(args.symbol, start_ms, end_ms, (2, 7, 14, 30))
        grids = {p: hourly_grid(pts, start_ms, end_ms) for p, pts in series.items()}
        r2 = grids[2]
        parts.append(f"snapshots={n_snap} hours={len(r2)} hours with a 2d ask={sum(x is not None for x in r2)}\n")
        for p in (7, 14, 30):
            parts.append(render_premium(f"{p}d best ask premium over 2d best ask", start_ms, r2, grids[p], "month"))
            parts.append(render_table(f"{p}d book (best visible exact-{p}d ask; None = not visible)", start_ms, r2, grids[p], p))

    report = "\n".join(parts)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain(sys.argv[1:])))
