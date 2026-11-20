"""Validate the trade-tape replay against our own offers (trade-tape plan, step 4).

Each of our submitted offers (rate, period, placement time, amount from
`submission_attempt_journal`, venue offer id from the transport/resolution
journals) is replayed against the public tape with our own trades removed, and
the replayed first-fill times are compared with the actual first fill from
`funding_trades`. Thresholds were pre-registered in the plan before the first
run (D4 = A, Will 2026-11-20):

1. bound coverage >= 80%: the actual first fill lies between the optimistic and
   conservative first fill. An offer with no fill within the wait counts as
   covered unless the conservative bound filled it.
2. queue-estimate error: median |estimate - actual| first fill <= 60 minutes,
   over offers with a book snapshot within 5 minutes of placement.
3. sample floor: >= 30 filled offers per period bucket.
4. below the floor, release on bound width alone: median width <= 4 hours.

A missing fill (actual or replayed) is pinned at placement + wait for the error
and width arithmetic, so "never filled" is the worst case, not a skipped row.

Run from backend/ (env: DATABASE_URL; read-only, the `bfx_research` role suffices):
  uv run python -m scripts.report_replay_validation --tape-root /tape [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import math
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.replay import (
    HypotheticalOffer,
    ReplayResult,
    Tape,
    TapeDays,
    replay_offer,
)
from bfx_funding_bot.modules.lending.tracking.queue_fill import queue_ahead_of
from bfx_funding_bot.modules.market.book_period_coverage import (
    BookAskSnapshot,
    snapshot_from_payload,
)

MS_PER_HOUR = 3_600_000
DEFAULT_MAX_WAIT_MS = 24 * MS_PER_HOUR  # replay_max_wait_h default
SNAPSHOT_TOLERANCE_MS = 5 * 60_000

# Pre-registered (plan step 4, Will 2026-11-20). Do not tune after a run.
COVERAGE_MIN = 0.80
QUEUE_ERROR_MEDIAN_MAX_MS = 60 * 60_000
SAMPLE_FLOOR = 30
WIDTH_MEDIAN_MAX_MS = 4 * MS_PER_HOUR


@dataclass(frozen=True)
class OwnOffer:
    venue_offer_id: str
    symbol: str
    rate: Decimal
    period_days: int
    placed_mts: int
    amount: Decimal
    first_fill_mts: int | None  # earliest own trade of this offer, any time


@dataclass(frozen=True)
class OfferOutcome:
    offer: OwnOffer
    actual_ms: int | None  # time to first fill, None when not within the wait
    optimistic_ms: int | None
    conservative_ms: int | None
    queue_ms: int | None
    has_queue_estimate: bool


class ExcludingTape(TapeDays):
    """A tape with the given trade ids removed: we never validate against ourselves."""

    def __init__(self, root: Path, symbol: str, exclude_ids: Iterable[int]) -> None:
        super().__init__(root, symbol)
        self._exclude = np.fromiter(exclude_ids, dtype=np.float64)

    def window(self, start_mts: int, end_mts: int) -> Tape:
        rows = super().window(start_mts, end_mts)
        if self._exclude.size == 0:
            return rows
        return rows[~np.isin(rows[:, 0], self._exclude)]


def _elapsed(first_mts: int | None, placed_mts: int, max_wait_ms: int) -> int | None:
    if first_mts is None:
        return None
    waited = first_mts - placed_mts
    return waited if 0 <= waited <= max_wait_ms else None


def nearest_snapshot(snapshots: Sequence[BookAskSnapshot], placed_mts: int,
                     tolerance_ms: int = SNAPSHOT_TOLERANCE_MS) -> BookAskSnapshot | None:
    """The latest snapshot at or before placement, else the earliest after, within tolerance."""
    before = [s for s in snapshots if placed_mts - tolerance_ms <= s.captured_at_ms <= placed_mts]
    if before:
        return max(before, key=lambda s: s.captured_at_ms)
    after = [s for s in snapshots if placed_mts < s.captured_at_ms <= placed_mts + tolerance_ms]
    return min(after, key=lambda s: s.captured_at_ms) if after else None


def same_rate_queue(snapshot: BookAskSnapshot, offer: OwnOffer) -> Decimal:
    """Asks at exactly our rate and period resting ahead of us (FIFO).

    Only the same-rate level: cheaper asks are consumed by trades below our rate,
    which the replay never counts. A snapshot taken after placement may already
    hold our own offer at that level, so its amount comes off (floored at zero).
    """
    # Same tolerance as replay_offer's same-rate test: payload rates went through float.
    level = [a for a in snapshot.asks if math.isclose(a[0], offer.rate, rel_tol=1e-9)]
    queue = queue_ahead_of(level, period_days=offer.period_days,
                           offer_rate=max((a[0] for a in level), default=offer.rate))
    if snapshot.captured_at_ms > offer.placed_mts:
        queue = max(queue - offer.amount, Decimal(0))
    return queue


def evaluate(offers: Sequence[OwnOffer], *, tape_for: Callable[[str], TapeDays],
             snapshots: Mapping[str, Sequence[BookAskSnapshot]],
             max_wait_ms: int = DEFAULT_MAX_WAIT_MS,
             tape_covers: Callable[[int], bool] = lambda _end_mts: True) -> list[OfferOutcome]:
    """Offers whose wait ends past the tape (`tape_covers` false) are left out, not failed."""
    outcomes = []
    for offer in offers:
        if not tape_covers(offer.placed_mts + max_wait_ms):
            continue
        snap = nearest_snapshot(snapshots.get(offer.symbol, ()), offer.placed_mts)
        queue = same_rate_queue(snap, offer) if snap is not None else None
        result: ReplayResult = replay_offer(
            HypotheticalOffer(rate=offer.rate, period_days=offer.period_days,
                              placed_mts=offer.placed_mts, amount=offer.amount),
            tape_for(offer.symbol), max_wait_ms=max_wait_ms, same_rate_queue_ahead=queue)
        p = offer.placed_mts
        outcomes.append(OfferOutcome(
            offer=offer,
            actual_ms=_elapsed(offer.first_fill_mts, p, max_wait_ms),
            optimistic_ms=_elapsed(result.optimistic.first_fill_mts, p, max_wait_ms),
            conservative_ms=_elapsed(result.conservative.first_fill_mts, p, max_wait_ms),
            queue_ms=(_elapsed(result.queue_estimate.first_fill_mts, p, max_wait_ms)
                      if result.queue_estimate is not None else None),
            has_queue_estimate=result.queue_estimate is not None,
        ))
    return outcomes


def is_covered(o: OfferOutcome) -> bool:
    if o.actual_ms is None:
        return o.conservative_ms is None
    if o.optimistic_ms is None or o.optimistic_ms > o.actual_ms:
        return False
    return o.conservative_ms is None or o.actual_ms <= o.conservative_ms


def _pin(ms: int | None, max_wait_ms: int) -> int:
    return max_wait_ms if ms is None else ms


def bound_width_ms(o: OfferOutcome, max_wait_ms: int) -> int:
    return _pin(o.conservative_ms, max_wait_ms) - _pin(o.optimistic_ms, max_wait_ms)


def queue_error_ms(o: OfferOutcome, max_wait_ms: int) -> int:
    """Signed: positive when the estimate fills later than reality."""
    return _pin(o.queue_ms, max_wait_ms) - _pin(o.actual_ms, max_wait_ms)


@dataclass(frozen=True)
class BucketSummary:
    period_days: int
    offers: int
    filled: int
    coverage: float
    width_median_ms: float
    queue_n: int
    queue_abs_error_median_ms: float | None
    queue_bias_median_ms: float | None
    floor_met: bool
    passed: bool


def summarize(outcomes: Sequence[OfferOutcome],
              max_wait_ms: int = DEFAULT_MAX_WAIT_MS) -> list[BucketSummary]:
    buckets: dict[int, list[OfferOutcome]] = defaultdict(list)
    for o in outcomes:
        buckets[o.offer.period_days].append(o)
    summaries = []
    for period, rows in sorted(buckets.items()):
        filled = sum(o.actual_ms is not None for o in rows)
        coverage = sum(is_covered(o) for o in rows) / len(rows)
        width = statistics.median(bound_width_ms(o, max_wait_ms) for o in rows)
        queued = [o for o in rows if o.has_queue_estimate]
        errors = [queue_error_ms(o, max_wait_ms) for o in queued]
        abs_med = statistics.median(abs(e) for e in errors) if errors else None
        bias = statistics.median(errors) if errors else None
        floor_met = filled >= SAMPLE_FLOOR
        if floor_met:
            passed = (coverage >= COVERAGE_MIN and abs_med is not None
                      and abs_med <= QUEUE_ERROR_MEDIAN_MAX_MS)
        else:
            passed = width <= WIDTH_MEDIAN_MAX_MS
        summaries.append(BucketSummary(period, len(rows), filled, coverage, width, len(queued),
                                       abs_med, bias, floor_met, passed))
    return summaries


def _minutes(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 60_000:.1f}"


def render(summaries: Sequence[BucketSummary], outcomes: Sequence[OfferOutcome],
           max_wait_ms: int = DEFAULT_MAX_WAIT_MS) -> str:
    lines = [
        "| period | offers | filled | floor (>= 30) | coverage (>= 80%) | width median min (<= 240) "
        "| queue n | abs error median min (<= 60) | bias median min | verdict |",
        "|---:|---:|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for s in summaries:
        verdict = "PASS" if s.passed else "FAIL"
        rule = "coverage+queue" if s.floor_met else "width only"
        lines.append(
            f"| {s.period_days} | {s.offers} | {s.filled} | {'met' if s.floor_met else 'not met'} "
            f"| {s.coverage:.1%} | {_minutes(s.width_median_ms)} | {s.queue_n} "
            f"| {_minutes(s.queue_abs_error_median_ms)} | {_minutes(s.queue_bias_median_ms)} "
            f"| {verdict} ({rule}) |")
    lines += ["", "| offer | placed (UTC) | rate | amount | actual | optimistic | conservative "
              "| queue | covered |", "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for o in outcomes:
        placed = time.strftime("%Y-%m-%d %H:%M", time.gmtime(o.offer.placed_mts / 1000))
        queue = _minutes(o.queue_ms) if o.has_queue_estimate else "n/a"
        lines.append(
            f"| {o.offer.venue_offer_id} | {placed} | {o.offer.rate} | {o.offer.amount} "
            f"| {_minutes(o.actual_ms)} | {_minutes(o.optimistic_ms)} "
            f"| {_minutes(o.conservative_ms)} | {queue} | {'yes' if is_covered(o) else 'no'} |")
    lines.append("")
    lines.append(f"Times are minutes from placement; — = no fill within {max_wait_ms // MS_PER_HOUR}h.")
    return "\n".join(lines) + "\n"


_OFFERS_SQL = text("""
WITH sub AS (
  SELECT s.attempt_id, s.symbol, s.started_at_ms, s.match_rate, s.match_period_days,
         s.intended_amount,
         coalesce(t.venue_offer_id, (
           SELECT r.venue_offer_id FROM execution_resolution_journal r
           WHERE r.attempt_id = s.attempt_id AND r.venue_offer_id IS NOT NULL
           ORDER BY r.resolved_at_ms LIMIT 1)) AS venue_offer_id
  FROM submission_attempt_journal s
  LEFT JOIN transport_outcome_journal t USING (attempt_id))
SELECT venue_offer_id, symbol, started_at_ms, match_rate, match_period_days, intended_amount
FROM sub
WHERE venue_offer_id IS NOT NULL AND match_rate IS NOT NULL AND match_period_days IS NOT NULL
ORDER BY started_at_ms
""")
_TRADES_SQL = text("SELECT offer_id, trade_id, mts_create FROM funding_trades")
_SNAPSHOTS_SQL = text(
    "SELECT symbol, captured_at_ms, payload FROM funding_book_snapshots "
    "WHERE captured_at_ms BETWEEN :lo AND :hi ORDER BY captured_at_ms")


async def load_inputs(session: AsyncSession) -> tuple[
        list[OwnOffer], list[int], dict[str, list[BookAskSnapshot]]]:
    trades = (await session.execute(_TRADES_SQL)).all()
    first_fill: dict[str, int] = {}
    for offer_id, _trade_id, mts in trades:
        key = str(offer_id)
        first_fill[key] = min(mts, first_fill.get(key, mts))
    offers = [
        OwnOffer(venue_offer_id=oid, symbol=sym, rate=rate, period_days=period,
                 placed_mts=placed, amount=amount, first_fill_mts=first_fill.get(oid))
        for oid, sym, placed, rate, period, amount in (await session.execute(_OFFERS_SQL)).all()
    ]
    # Trade ids are one venue-wide sequence, so every own trade leaves every tape.
    own_ids = [trade_id for _offer_id, trade_id, _mts in trades]
    snapshots: dict[str, list[BookAskSnapshot]] = defaultdict(list)
    if offers:
        rows = await session.execute(_SNAPSHOTS_SQL, {
            "lo": offers[0].placed_mts - SNAPSHOT_TOLERANCE_MS,
            "hi": offers[-1].placed_mts + SNAPSHOT_TOLERANCE_MS})
        for sym, ts, payload in rows.all():
            snapshots[sym].append(snapshot_from_payload(symbol=sym, captured_at_ms=ts,
                                                        payload=payload))
    return offers, own_ids, snapshots


def tape_has_day(root: Path, offers: Sequence[OwnOffer], end_mts: int) -> bool:
    day = time.strftime("%Y-%m-%d", time.gmtime(end_mts / 1000))
    return all((root / sym / f"{day}.csv.gz").is_file() for sym in {o.symbol for o in offers})


async def _amain(args: argparse.Namespace) -> int:
    engine = make_engine(Settings())
    try:
        async with make_session_factory(engine)() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            offers, own_ids, snapshots = await load_inputs(session)
    finally:
        await engine.dispose()
    tape_root = Path(args.tape_root)
    tapes = {sym: ExcludingTape(tape_root, sym, own_ids)
             for sym in {o.symbol for o in offers}}
    outcomes = evaluate(offers, tape_for=tapes.__getitem__, snapshots=snapshots,
                        tape_covers=lambda end: tape_has_day(tape_root, offers, end))
    report = render(summarize(outcomes), outcomes)
    skipped = len(offers) - len(outcomes)
    report += f"\nSkipped {skipped} offer(s) whose wait ends after the last tape day.\n"
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tape-root", required=True)
    parser.add_argument("--out")
    return asyncio.run(_amain(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
