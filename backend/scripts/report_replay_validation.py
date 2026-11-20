r"""Validate the trade-tape replay against our own offers (trade-tape plan, step 4).

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

Post-hoc changes after the first run failed (Will 2026-11-20, plan step 4 D6;
the thresholds above are unchanged):

- placement is the venue ack (`transport_outcome_journal.completed_at_ms`), not
  the start of the send, and coverage allows the tape's 1-second resolution;
- only trades of our *other* offers leave the tape: the borrower who took the
  replayed offer is demand that exists either way (at our rate it reaches only
  the optimistic bound and the queue estimate, so the conservative bound stays
  independent of our own fills);
- bids resting at placement that the offer crosses fill it at once
  (`replay_offer(crossing_bid_amount=...)`, from the snapshot at or before
  placement);
- `funding_trades` misses fills where our offer took a resting bid, so the
  actual first fill and the cancel time also come from the offer history
  (`--offer-history`, exported with `bfx_operator`, which alone can read it):
    \copy (SELECT venue_offer_id, amount_original, amount_remaining, terminal_kind,
            mts_updated FROM ledger_observation_offer_history) TO 'history.csv' CSV HEADER
  A cancelled offer is replayed only until its cancel; an offer with no fill and
  no history has an unknown end and is left out (counted in the report).

Run from backend/ (env: DATABASE_URL; read-only, the `bfx_research` role suffices):
  uv run python -m scripts.report_replay_validation --tape-root /tape [--out FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
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

MS_PER_HOUR = 3_600_000
DEFAULT_MAX_WAIT_MS = 24 * MS_PER_HOUR  # replay_max_wait_h default
SNAPSHOT_TOLERANCE_MS = 5 * 60_000
COVERAGE_TOLERANCE_MS = 1_000  # tape timestamps are whole seconds

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
    first_fill_mts: int | None  # earliest own fill of this offer, any time
    own_trade_ids: tuple[int, ...] = ()
    cancel_mts: int | None = None  # stopped resting unfilled at this time
    end_known: bool = True  # False: no fill and no history, so the wait is unknown


@dataclass(frozen=True)
class BookSides:
    captured_at_ms: int
    asks: tuple[tuple[Decimal, int, Decimal], ...]
    bids: tuple[tuple[Decimal, int, Decimal], ...]


@dataclass(frozen=True)
class OfferOutcome:
    offer: OwnOffer
    actual_ms: int | None  # time to first fill, None when not within the wait
    optimistic_ms: int | None
    conservative_ms: int | None
    queue_ms: int | None
    has_queue_estimate: bool
    wait_ms: int = DEFAULT_MAX_WAIT_MS


class ExcludingTape(TapeDays):
    """A tape without our own trades, except those `keep` names (the replayed offer's)."""

    def __init__(self, root: Path, symbol: str, exclude_ids: Iterable[int]) -> None:
        super().__init__(root, symbol)
        self._all_own = frozenset(exclude_ids)
        self._exclude = np.fromiter(self._all_own, dtype=np.float64)

    def keep(self, ids: Iterable[int]) -> None:
        self._exclude = np.fromiter(self._all_own - set(ids), dtype=np.float64)

    def window(self, start_mts: int, end_mts: int) -> Tape:
        rows = super().window(start_mts, end_mts)
        if self._exclude.size == 0:
            return rows
        return rows[~np.isin(rows[:, 0], self._exclude)]


def book_from_payload(captured_at_ms: int, payload: Mapping[str, object]) -> BookSides:
    """`funding_book_snapshots.payload`: {"asks"|"bids": [[rate, period, count, amount]]}."""
    def side(key: str) -> tuple[tuple[Decimal, int, Decimal], ...]:
        raw = payload.get(key) or []
        assert isinstance(raw, list)
        return tuple((Decimal(str(r)), int(p), Decimal(str(a))) for r, p, _c, a in raw)
    return BookSides(captured_at_ms, side("asks"), side("bids"))


def _elapsed(first_mts: int | None, placed_mts: int, max_wait_ms: int) -> int | None:
    if first_mts is None:
        return None
    waited = first_mts - placed_mts
    # Venue times are whole seconds, the ack is not: a fill stamped in the ack's
    # own second can read up to a second before placement.
    if -COVERAGE_TOLERANCE_MS < waited < 0:
        waited = 0
    return waited if 0 <= waited <= max_wait_ms else None


def nearest_snapshot(snapshots: Sequence[BookSides], placed_mts: int,
                     tolerance_ms: int = SNAPSHOT_TOLERANCE_MS) -> BookSides | None:
    """The latest snapshot at or before placement, else the earliest after, within tolerance."""
    before = [s for s in snapshots if placed_mts - tolerance_ms <= s.captured_at_ms <= placed_mts]
    if before:
        return max(before, key=lambda s: s.captured_at_ms)
    after = [s for s in snapshots if placed_mts < s.captured_at_ms <= placed_mts + tolerance_ms]
    return min(after, key=lambda s: s.captured_at_ms) if after else None


def same_rate_queue(snapshot: BookSides, offer: OwnOffer) -> Decimal:
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


def crossing_bids(snapshot: BookSides, offer: OwnOffer) -> Decimal:
    """Bid volume the offer would match on arrival: rate >= ours, period <= ours.

    Only a snapshot from before placement can show it: after placement the
    crossed bids are already gone.
    """
    if snapshot.captured_at_ms > offer.placed_mts:
        return Decimal(0)
    floor = offer.rate * (1 - Decimal("1e-9"))
    return sum((a for r, p, a in snapshot.bids if r >= floor and p <= offer.period_days),
               Decimal(0))


def offer_wait_ms(offer: OwnOffer, max_wait_ms: int) -> int:
    if offer.first_fill_mts is None and offer.cancel_mts is not None:
        return min(max_wait_ms, offer.cancel_mts - offer.placed_mts)
    return max_wait_ms


def evaluate(offers: Sequence[OwnOffer], *, tape_for: Callable[[str], TapeDays],
             snapshots: Mapping[str, Sequence[BookSides]],
             max_wait_ms: int = DEFAULT_MAX_WAIT_MS,
             tape_covers: Callable[[int], bool] = lambda _end_mts: True) -> list[OfferOutcome]:
    """Replay every offer with a known end over its own wait.

    Left out, not failed: offers whose wait ends past the tape (`tape_covers`
    false), offers with an unknown end, and offers cancelled before they rested.
    """
    outcomes = []
    for offer in offers:
        wait = offer_wait_ms(offer, max_wait_ms)
        if not offer.end_known or wait <= 0 or not tape_covers(offer.placed_mts + wait):
            continue
        snap = nearest_snapshot(snapshots.get(offer.symbol, ()), offer.placed_mts)
        queue = same_rate_queue(snap, offer) if snap is not None else None
        crossing = crossing_bids(snap, offer) if snap is not None else None
        tape = tape_for(offer.symbol)
        if isinstance(tape, ExcludingTape):
            tape.keep(offer.own_trade_ids)
        result: ReplayResult = replay_offer(
            HypotheticalOffer(rate=offer.rate, period_days=offer.period_days,
                              placed_mts=offer.placed_mts, amount=offer.amount),
            tape, max_wait_ms=wait, same_rate_queue_ahead=queue, crossing_bid_amount=crossing)
        p = offer.placed_mts
        outcomes.append(OfferOutcome(
            offer=offer,
            actual_ms=_elapsed(offer.first_fill_mts, p, wait),
            optimistic_ms=_elapsed(result.optimistic.first_fill_mts, p, wait),
            conservative_ms=_elapsed(result.conservative.first_fill_mts, p, wait),
            queue_ms=(_elapsed(result.queue_estimate.first_fill_mts, p, wait)
                      if result.queue_estimate is not None else None),
            has_queue_estimate=result.queue_estimate is not None,
            wait_ms=wait,
        ))
    return outcomes


def is_covered(o: OfferOutcome) -> bool:
    if o.actual_ms is None:
        return o.conservative_ms is None
    tol = COVERAGE_TOLERANCE_MS
    if o.optimistic_ms is None or o.optimistic_ms > o.actual_ms + tol:
        return False
    return o.conservative_ms is None or o.actual_ms <= o.conservative_ms + tol


def _pin(ms: int | None, wait_ms: int) -> int:
    return wait_ms if ms is None else ms


def bound_width_ms(o: OfferOutcome) -> int:
    return _pin(o.conservative_ms, o.wait_ms) - _pin(o.optimistic_ms, o.wait_ms)


def queue_error_ms(o: OfferOutcome) -> int:
    """Signed: positive when the estimate fills later than reality."""
    return _pin(o.queue_ms, o.wait_ms) - _pin(o.actual_ms, o.wait_ms)


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


def summarize(outcomes: Sequence[OfferOutcome]) -> list[BucketSummary]:
    buckets: dict[int, list[OfferOutcome]] = defaultdict(list)
    for o in outcomes:
        buckets[o.offer.period_days].append(o)
    summaries = []
    for period, rows in sorted(buckets.items()):
        filled = sum(o.actual_ms is not None for o in rows)
        coverage = sum(is_covered(o) for o in rows) / len(rows)
        width = statistics.median(bound_width_ms(o) for o in rows)
        queued = [o for o in rows if o.has_queue_estimate]
        errors = [queue_error_ms(o) for o in queued]
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
    lines.append(f"Times are minutes from placement; — = no fill within the offer's wait "
                 f"({max_wait_ms // MS_PER_HOUR}h, or until its cancel).")
    return "\n".join(lines) + "\n"


_OFFERS_SQL = text("""
WITH sub AS (
  SELECT s.attempt_id, s.symbol, coalesce(t.completed_at_ms, s.started_at_ms) AS placed_mts,
         s.match_rate, s.match_period_days, s.intended_amount,
         coalesce(t.venue_offer_id, (
           SELECT r.venue_offer_id FROM execution_resolution_journal r
           WHERE r.attempt_id = s.attempt_id AND r.venue_offer_id IS NOT NULL
           ORDER BY r.resolved_at_ms LIMIT 1)) AS venue_offer_id
  FROM submission_attempt_journal s
  LEFT JOIN transport_outcome_journal t USING (attempt_id))
SELECT venue_offer_id, symbol, placed_mts, match_rate, match_period_days, intended_amount
FROM sub
WHERE venue_offer_id IS NOT NULL AND match_rate IS NOT NULL AND match_period_days IS NOT NULL
ORDER BY placed_mts
""")
_TRADES_SQL = text("SELECT offer_id, trade_id, mts_create FROM funding_trades")
_SNAPSHOTS_SQL = text(
    "SELECT symbol, captured_at_ms, payload FROM funding_book_snapshots "
    "WHERE captured_at_ms BETWEEN :lo AND :hi ORDER BY captured_at_ms")


@dataclass(frozen=True)
class HistoryRow:
    venue_offer_id: str
    amount_original: Decimal | None
    amount_remaining: Decimal
    terminal_kind: str
    mts_updated: int | None


def read_history(path: Path) -> list[HistoryRow]:
    with path.open(newline="", encoding="utf-8") as stream:
        return [HistoryRow(r["venue_offer_id"],
                           Decimal(r["amount_original"]) if r["amount_original"] else None,
                           Decimal(r["amount_remaining"]), r["terminal_kind"],
                           int(r["mts_updated"]) if r["mts_updated"] else None)
                for r in csv.DictReader(stream)]


@dataclass(frozen=True)
class HistoryFacts:
    first_fill_mts: int | None
    cancel_mts: int | None


def history_facts(rows: Iterable[HistoryRow]) -> dict[str, HistoryFacts]:
    """Per offer: the first observation with less left than placed, and a clean cancel."""
    fills: dict[str, int] = {}
    cancels: dict[str, int] = {}
    seen: set[str] = set()
    for r in rows:
        seen.add(r.venue_offer_id)
        if r.mts_updated is None:
            continue
        filled = (r.terminal_kind == "executed"
                  or (r.amount_original is not None and r.amount_remaining < r.amount_original))
        if filled:
            fills[r.venue_offer_id] = min(r.mts_updated, fills.get(r.venue_offer_id, r.mts_updated))
        elif r.terminal_kind == "canceled":
            cancels[r.venue_offer_id] = max(r.mts_updated, cancels.get(r.venue_offer_id, 0))
    return {oid: HistoryFacts(fills.get(oid), cancels.get(oid)) for oid in seen}


def build_offers(offer_rows: Iterable[tuple[str, str, int, Decimal, int, Decimal]],
                 trades: Iterable[tuple[int, int, int]],
                 history: Mapping[str, HistoryFacts]) -> list[OwnOffer]:
    trade_fill: dict[str, int] = {}
    trade_ids: dict[str, list[int]] = defaultdict(list)
    for offer_id, trade_id, mts in trades:
        key = str(offer_id)
        trade_fill[key] = min(mts, trade_fill.get(key, mts))
        trade_ids[key].append(trade_id)
    offers = []
    for oid, sym, placed, rate, period, amount in offer_rows:
        facts = history.get(oid)
        fills = [m for m in (trade_fill.get(oid), facts and facts.first_fill_mts) if m]
        first = min(fills) if fills else None
        offers.append(OwnOffer(
            venue_offer_id=oid, symbol=sym, rate=rate, period_days=period, placed_mts=placed,
            amount=amount, first_fill_mts=first, own_trade_ids=tuple(trade_ids.get(oid, ())),
            cancel_mts=facts.cancel_mts if facts else None,
            end_known=first is not None or facts is not None))
    return offers


async def load_inputs(session: AsyncSession, history: Mapping[str, HistoryFacts]) -> tuple[
        list[OwnOffer], list[int], dict[str, list[BookSides]]]:
    trades = [tuple(r) for r in (await session.execute(_TRADES_SQL)).all()]
    offers = build_offers([tuple(r) for r in (await session.execute(_OFFERS_SQL)).all()],
                          trades, history)
    # Trade ids are one venue-wide sequence, so every own trade leaves every tape.
    own_ids = [int(trade_id) for _offer_id, trade_id, _mts in trades]
    snapshots: dict[str, list[BookSides]] = defaultdict(list)
    if offers:
        rows = await session.execute(_SNAPSHOTS_SQL, {
            "lo": offers[0].placed_mts - SNAPSHOT_TOLERANCE_MS,
            "hi": offers[-1].placed_mts + SNAPSHOT_TOLERANCE_MS})
        for sym, ts, payload in rows.all():
            snapshots[sym].append(book_from_payload(ts, payload))
    return offers, own_ids, snapshots


def tape_has_day(root: Path, offers: Sequence[OwnOffer], end_mts: int) -> bool:
    day = time.strftime("%Y-%m-%d", time.gmtime(end_mts / 1000))
    return all((root / sym / f"{day}.csv.gz").is_file() for sym in {o.symbol for o in offers})


async def _amain(args: argparse.Namespace) -> int:
    history = history_facts(read_history(Path(args.offer_history)))
    engine = make_engine(Settings())
    try:
        async with make_session_factory(engine)() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            offers, own_ids, snapshots = await load_inputs(session, history)
    finally:
        await engine.dispose()
    tape_root = Path(args.tape_root)
    tapes = {sym: ExcludingTape(tape_root, sym, own_ids)
             for sym in {o.symbol for o in offers}}
    outcomes = evaluate(offers, tape_for=tapes.__getitem__, snapshots=snapshots,
                        tape_covers=lambda end: tape_has_day(tape_root, offers, end))
    report = render(summarize(outcomes), outcomes)
    unknown = sum(not o.end_known for o in offers)
    report += (f"\nLeft out: {len(offers) - len(outcomes)} of {len(offers)} offer(s) "
               f"({unknown} with an unknown end; the rest end past the last tape day "
               f"or were cancelled before resting).\n")
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tape-root", required=True)
    parser.add_argument("--offer-history", required=True,
                        help="CSV exported with bfx_operator (see module docstring)")
    parser.add_argument("--out")
    return asyncio.run(_amain(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
