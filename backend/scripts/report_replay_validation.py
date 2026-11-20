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

Second post-hoc round (Will 2026-11-20, after review round 2; thresholds still
unchanged):

- the replay window opens at the start of the ack's whole second, since the
  venue stamps a fill in that second at or before the ack;
- the offer history only bounds the first fill from above (`mts_updated` is the
  last update of a row that already shows a fill); it sets the first fill only
  when that bound lies within the ack's second, otherwise an offer missing from
  `funding_trades` is left out as "fill time unknown";
- a cancel counts only when the row shows nothing filled (`amount_original`
  known and equal to the remainder); a row with no time or a cancel with no
  original amount leaves an unfilled offer's end unknown;
- placement is only an `ack` transport outcome; other outcomes record when the
  send gave up, not when the venue accepted, so those offers are left out;
- crossed bids come only from a snapshot taken at most 10 s before placement.

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
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
CROSSING_SNAPSHOT_MAX_AGE_MS = 10_000  # resting bids turn over within minutes

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
    excluded: str | None = None  # why the offer cannot be replayed, if it cannot


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
    """A tape without the given trade ids (our own trades, minus the replayed offer's)."""

    def __init__(self, root: Path, symbol: str, exclude_ids: Iterable[int]) -> None:
        super().__init__(root, symbol)
        self._exclude = np.fromiter(frozenset(exclude_ids), dtype=np.float64)

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


def replay_start_mts(placed_mts: int) -> int:
    """Just before the ack's whole second: `window` is strict and venue times are whole seconds."""
    return placed_mts - placed_mts % 1000 - 1


def in_ack_second(mts: int, placed_mts: int) -> bool:
    """`mts` falls in the ack's whole second (before or after the ack itself)."""
    return 0 <= mts - (placed_mts - placed_mts % 1000) < 1000


def _elapsed(first_mts: int | None, placed_mts: int, max_wait_ms: int, *,
             replayed: bool = False) -> int | None:
    if first_mts is None:
        return None
    waited = first_mts - placed_mts
    # A fill stamped in the ack's own second reads as before the ack; a replayed
    # fill can also sit at the window's opening millisecond (crossed bids).
    if waited < 0 and (in_ack_second(first_mts, placed_mts)
                       or (replayed and first_mts == replay_start_mts(placed_mts))):
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

    Only a snapshot from just before placement can show it: after placement the
    crossed bids are gone, and an older one shows bids that may have left.
    """
    age = offer.placed_mts - snapshot.captured_at_ms
    if not 0 <= age <= CROSSING_SNAPSHOT_MAX_AGE_MS:
        return Decimal(0)
    floor = offer.rate * (1 - Decimal("1e-9"))
    return sum((a for r, p, a in snapshot.bids if r >= floor and p <= offer.period_days),
               Decimal(0))


def offer_wait_ms(offer: OwnOffer, max_wait_ms: int) -> int:
    if offer.first_fill_mts is None and offer.cancel_mts is not None:
        return min(max_wait_ms, offer.cancel_mts - offer.placed_mts)
    return max_wait_ms


def evaluate(offers: Sequence[OwnOffer], *, tape_for: Callable[[OwnOffer], TapeDays],
             snapshots: Mapping[str, Sequence[BookSides]],
             max_wait_ms: int = DEFAULT_MAX_WAIT_MS,
             tape_covers: Callable[[str, int, int], bool] = lambda _s, _a, _b: True,
             ) -> list[OfferOutcome]:
    """Replay every offer that can be replayed over its own wait.

    Left out, not failed: offers with an `excluded` reason, offers whose wait
    is not fully on the tape (`tape_covers(symbol, start, end)` false), and
    offers cancelled before they rested.
    """
    outcomes = []
    for offer in offers:
        wait = offer_wait_ms(offer, max_wait_ms)
        start = replay_start_mts(offer.placed_mts)
        if (offer.excluded is not None or wait <= 0
                or not tape_covers(offer.symbol, start, offer.placed_mts + wait)):
            continue
        snap = nearest_snapshot(snapshots.get(offer.symbol, ()), offer.placed_mts)
        queue = same_rate_queue(snap, offer) if snap is not None else None
        crossing = crossing_bids(snap, offer) if snap is not None else None
        result: ReplayResult = replay_offer(
            HypotheticalOffer(rate=offer.rate, period_days=offer.period_days,
                              placed_mts=start, amount=offer.amount),
            tape_for(offer), max_wait_ms=wait + offer.placed_mts - start,
            same_rate_queue_ahead=queue, crossing_bid_amount=crossing)
        p = offer.placed_mts
        outcomes.append(OfferOutcome(
            offer=offer,
            actual_ms=_elapsed(offer.first_fill_mts, p, wait),
            optimistic_ms=_elapsed(result.optimistic.first_fill_mts, p, wait, replayed=True),
            conservative_ms=_elapsed(result.conservative.first_fill_mts, p, wait, replayed=True),
            queue_ms=(_elapsed(result.queue_estimate.first_fill_mts, p, wait, replayed=True)
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
         t.kind IS NOT NULL AS acked,
         s.match_rate, s.match_period_days, s.intended_amount,
         coalesce(t.venue_offer_id, (
           SELECT r.venue_offer_id FROM execution_resolution_journal r
           WHERE r.attempt_id = s.attempt_id AND r.venue_offer_id IS NOT NULL
           ORDER BY r.resolved_at_ms LIMIT 1)) AS venue_offer_id
  FROM submission_attempt_journal s
  LEFT JOIN transport_outcome_journal t ON t.attempt_id = s.attempt_id AND t.kind = 'ack')
SELECT venue_offer_id, symbol, placed_mts, match_rate, match_period_days, intended_amount, acked
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
    fill_by_mts: int | None  # the first fill happened at or before this
    cancel_mts: int | None  # cancelled with nothing filled
    complete: bool  # every row has a time, and every cancel a known original amount


def history_facts(rows: Iterable[HistoryRow]) -> dict[str, HistoryFacts]:
    """Per offer: an upper bound on the first fill, a clean cancel, and whether rows are whole.

    `mts_updated` is a row's last update, so a row that shows a fill only says
    the first fill came no later than that time.
    """
    fills: dict[str, int] = {}
    cancels: dict[str, int] = {}
    seen: set[str] = set()
    incomplete: set[str] = set()
    for r in rows:
        oid = r.venue_offer_id
        seen.add(oid)
        if r.mts_updated is None:
            incomplete.add(oid)
            continue
        if r.terminal_kind == "executed" or (
                r.amount_original is not None and r.amount_remaining < r.amount_original):
            fills[oid] = min(r.mts_updated, fills.get(oid, r.mts_updated))
        elif r.terminal_kind == "canceled":
            if r.amount_original is None:
                incomplete.add(oid)  # cannot tell whether part of it filled
            else:
                cancels[oid] = max(r.mts_updated, cancels.get(oid, 0))
    return {oid: HistoryFacts(fills.get(oid), cancels.get(oid), oid not in incomplete)
            for oid in seen}


def build_offers(offer_rows: Iterable[tuple[str, str, int, Decimal, int, Decimal, bool]],
                 trades: Iterable[tuple[int, int, int]],
                 history: Mapping[str, HistoryFacts]) -> list[OwnOffer]:
    """Join the journal, `funding_trades` and the offer history into replayable offers.

    Trades give exact fill times. The history adds a fill missing from the
    trades only when its upper bound lies within the ack's second, where it is
    exact to the tape's resolution; a later bound leaves the fill time unknown.
    """
    trade_fill: dict[str, int] = {}
    trade_ids: dict[str, list[int]] = defaultdict(list)
    for offer_id, trade_id, mts in trades:
        key = str(offer_id)
        trade_fill[key] = min(mts, trade_fill.get(key, mts))
        trade_ids[key].append(trade_id)
    offers = []
    for oid, sym, placed, rate, period, amount, acked in offer_rows:
        facts = history.get(oid)
        first = trade_fill.get(oid)
        by = facts.fill_by_mts if facts else None
        reasons = [] if acked else ["no venue ack"]
        if by is not None and (first is None or by < first):
            if in_ack_second(by, placed):
                first = by
            else:
                reasons.append("fill time unknown")
        if first is None and (facts is None or not facts.complete):
            reasons.append("unknown end")
        offers.append(OwnOffer(
            venue_offer_id=oid, symbol=sym, rate=rate, period_days=period, placed_mts=placed,
            amount=amount, first_fill_mts=first, own_trade_ids=tuple(trade_ids.get(oid, ())),
            cancel_mts=facts.cancel_mts if facts else None,
            excluded=reasons[0] if reasons else None))  # first reason only, in check order
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


def tape_has_days(root: Path, symbol: str, start_mts: int, end_mts: int) -> bool:
    """Every UTC day file of `symbol` from `start_mts` through `end_mts` exists."""
    day, last = (datetime.fromtimestamp(m / 1000, UTC).date() for m in (start_mts, end_mts))
    while day <= last:
        if not (root / symbol / f"{day.isoformat()}.csv.gz").is_file():
            return False
        day += timedelta(days=1)
    return True


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
    all_own = frozenset(own_ids)
    outcomes = evaluate(
        offers, snapshots=snapshots,
        tape_for=lambda o: ExcludingTape(tape_root, o.symbol, all_own - set(o.own_trade_ids)),
        tape_covers=lambda sym, a, b: tape_has_days(tape_root, sym, a, b))
    report = render(summarize(outcomes), outcomes)
    reasons = Counter(o.excluded for o in offers if o.excluded is not None)
    listed = ", ".join(f"{n} {why}" for why, n in sorted(reasons.items())) or "none excluded"
    report += (f"\nLeft out: {len(offers) - len(outcomes)} of {len(offers)} offer(s) "
               f"({listed}; the rest are not fully on the tape or were cancelled "
               f"before resting).\n")
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
