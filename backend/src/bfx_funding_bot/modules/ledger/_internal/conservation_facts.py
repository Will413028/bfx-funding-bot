"""Id-level facts of one interval between two accepted observations, as verdict inputs.

Previous accepted basis P, this observation C. Credits are keyed per lending, fills
per offer id (``ledger/conservation.py`` has the rule and why). Time is used in two
places only, both unavoidable and both widened by the venue clock tolerance:

* an offer that is in neither P's nor C's offers but in C's terminal history is
  new in the interval only if it ended after P's query started (older ones are
  already in P's credits);
* C's funding trades cross-check each offer's remaining-derived fill: trades after
  P's observation finished (plus tolerance) are certainly new, trades after P's
  query started (less tolerance) possibly new, so the fill must lie between the
  two sums. The lower bound is checked only when C's trades window reaches back
  to P's finish, the upper only when it reaches back to P's start.

Own or foreign: an offer is foreign only when no attempt names it and it is not a
member of an unresolved quarantine (a quarantine member is not known to be
foreign). The split changes the alert, never the sum.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.venue_time import VENUE_CLOCK_TOLERANCE_MS
from bfx_funding_bot.modules.ledger.conservation import (
    LEDGER_EPSILON,
    ConservationVerdict,
    OfferFill,
    conservation_verdict,
)
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    LedgerObservationCreditRow,
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    LedgerObservationTradeRow,
    QuarantineMemberRow,
)

ZERO = Decimal(0)


def credit_key(
    source_kind: str, venue_credit_id: str, period_days: int | None, mts_opening: int | None
) -> str:
    """One lending: the venue keeps (period, opening) when it turns a loan into credits
    or splits and merges them, and changes the ids; without them the id is all there is."""
    if period_days is not None and mts_opening is not None:
        return f"{period_days}|{mts_opening}"
    return f"id|{source_kind}|{venue_credit_id}"


def _credits(
    rows: Sequence[AcceptedCapitalBasisCreditRow] | Sequence[LedgerObservationCreditRow],
) -> dict[str, dict[str, Decimal]]:
    by_symbol: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in rows:
        key = credit_key(row.source_kind, row.venue_credit_id, row.period_days, row.mts_opening)
        by_symbol[row.symbol][key] = by_symbol[row.symbol].get(key, ZERO) + row.amount
    return by_symbol


async def symbol_verdicts(
    session: AsyncSession,
    *,
    previous: AcceptedCapitalBasisRow | None,
    observation: LedgerObservationRow,
    symbols: Collection[str],
    previous_offers: Sequence[LedgerObservationOfferRow],
    previous_credits: Sequence[AcceptedCapitalBasisCreditRow],
    offers: Sequence[LedgerObservationOfferRow],
    credits: Sequence[LedgerObservationCreditRow],
    terminal: Mapping[str, LedgerObservationOfferHistoryRow],
    trades: Sequence[LedgerObservationTradeRow],
    provenance: Mapping[str, set[UUID]],
    quarantines: Sequence[UUID],
) -> dict[str, ConservationVerdict]:
    """Each wallet symbol's verdict; a symbol without a row in P is a baseline."""
    if previous is None:
        return {name: conservation_verdict(None, {}, []) for name in symbols}
    prior_symbols = set(
        await session.scalars(
            select(AcceptedCapitalBasisSymbolRow.symbol).where(
                AcceptedCapitalBasisSymbolRow.basis_id == previous.id
            )
        )
    )
    started_ms = await session.scalar(
        select(LedgerObservationQueryRow.started_at_ms)
        .join(
            LedgerObservationRow, LedgerObservationRow.query_id == LedgerObservationQueryRow.query_id
        )
        .where(LedgerObservationRow.id == previous.observation_id)
    )
    finished_ms = await session.scalar(
        select(LedgerObservationRow.confirmation_finished_at_ms).where(
            LedgerObservationRow.id == previous.observation_id
        )
    )
    assert started_ms is not None and finished_ms is not None

    possible_after = started_ms - VENUE_CLOCK_TOLERANCE_MS
    certain_after = finished_ms + VENUE_CLOCK_TOLERANCE_MS
    start = observation.trades_requested_start_ms
    covers_lower = observation.trades_complete and start is not None and start <= certain_after
    covers_upper = observation.trades_complete and start is not None and start <= possible_after
    possible: dict[str, Decimal] = defaultdict(lambda: ZERO)
    certain: dict[str, Decimal] = defaultdict(lambda: ZERO)
    trade_symbol: dict[str, str] = {}
    for trade in trades:
        if trade.mts_create > possible_after:
            possible[trade.venue_offer_id] += trade.amount
            trade_symbol[trade.venue_offer_id] = trade.symbol
        if trade.mts_create > certain_after:
            certain[trade.venue_offer_id] += trade.amount

    before = {offer.venue_offer_id: offer for offer in previous_offers}
    now = {offer.venue_offer_id: offer for offer in offers}
    ended = {
        offer_id: row
        for offer_id, row in terminal.items()
        if offer_id in before or row.occurred_at_ms > started_ms
    }
    ids = set(before) | set(now) | set(ended) | {i for i, amount in certain.items() if amount > 0}
    held = (
        set(
            await session.scalars(
                select(QuarantineMemberRow.venue_object_id).where(
                    QuarantineMemberRow.quarantine_id.in_(list(quarantines)),
                    QuarantineMemberRow.source_kind == "offer",
                    QuarantineMemberRow.venue_object_id.in_(sorted(ids)),
                )
            )
        )
        if quarantines and ids
        else set()
    )

    fills: dict[str, list[OfferFill]] = defaultdict(list)
    for offer_id in sorted(ids):
        symbol: str
        original: Decimal | None
        remaining: Decimal | None
        if offer_id in now:
            symbol, original = now[offer_id].symbol, now[offer_id].amount_original
            remaining = now[offer_id].amount_remaining
        elif offer_id in ended:
            symbol, original = ended[offer_id].symbol, ended[offer_id].amount_original
            remaining = ended[offer_id].amount_remaining
        else:
            # Gone without a trace in the history, or known only by a trade: its end is unknown.
            symbol = before[offer_id].symbol if offer_id in before else trade_symbol[offer_id]
            original, remaining = None, None
        start_remaining = before[offer_id].amount_remaining if offer_id in before else original
        foreign = not provenance.get(offer_id) and offer_id not in held
        if start_remaining is None or remaining is None:
            fills[symbol].append(OfferFill(ZERO, foreign, conflict=True))
            continue
        filled = start_remaining - remaining
        disagree = (covers_lower and certain[offer_id] > filled + LEDGER_EPSILON) or (
            covers_upper and filled > possible[offer_id] + LEDGER_EPSILON
        )
        fills[symbol].append(OfferFill(filled, foreign, conflict=bool(disagree)))

    prior = _credits(previous_credits)
    current = _credits(credits)
    return {
        name: conservation_verdict(
            prior.get(name, {}) if name in prior_symbols else None,
            current.get(name, {}),
            fills.get(name, []),
        )
        for name in symbols
    }


__all__ = ["credit_key", "symbol_verdicts"]
