"""Classify an accepted observation into the capital basis (venue facts only).

Ports legacy ``CapitalRepository._classify`` onto the journals and the
observation store. Policy is not read here: it is applied per symbol when the
basis is read. Every query is bounded by the current observation, the previous
basis, or the attempts recorded after the previous basis's
``attempt_seq_high_water``.

A cell's amount is the remaining amount of its resting managed offers, and
nothing else. Lent credits count in their symbol's ``credits`` only: the venue
gives no credit-to-offer link, so no cell holds lent stock
(docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md).

Provenance of a venue offer (``read.provenance``): a transport ``ack``
outcome or a ``bound_to_venue`` resolution names it -> the attempt. No
provenance means the offer is foreign; more than one story is a fact-level
block, never a reason to call it foreign.

Blocks are per symbol (``accepted_capital_basis_symbol.block``); a fact that
cannot be tied to a symbol row of this basis goes to ``scope_block``. Each symbol
row also stores its conservation verdict against the previous accepted basis
(``ledger/conservation.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.venue_time import HISTORY_QUERY_MARGIN_MS, VENUE_CLOCK_TOLERANCE_MS
from bfx_funding_bot.modules.ledger import JsonObject, Quarantine, Scope
from bfx_funding_bot.modules.ledger._internal import unconfirmed_ends
from bfx_funding_bot.modules.ledger._internal.conservation_facts import (
    OfferEndOutcome,
    symbol_verdicts,
)
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload
from bfx_funding_bot.modules.ledger._internal.quarantine import open_quarantine
from bfx_funding_bot.modules.ledger.conservation import ConservationVerdict
from bfx_funding_bot.modules.ledger.read import history_symbols
from bfx_funding_bot.modules.ledger.read.basis import previous_basis
from bfx_funding_bot.modules.ledger.read.provenance import offer_provenance, sole_owner
from bfx_funding_bot.modules.ledger.read.quarantine import unresolved_quarantines
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisCellRow,
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisQuarantineRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    ExecutionResolutionJournalRow,
    LedgerObservationCreditHistoryRow,
    LedgerObservationCreditRow,
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationRow,
    LedgerObservationTradeRow,
    LedgerObservationWalletRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)
SCHEMA_VERSION = 2
ZERO = Decimal(0)


@dataclass(slots=True)
class _Symbol:
    available: Decimal
    offered: Decimal = ZERO
    credits: Decimal = ZERO
    foreign: Decimal = ZERO
    reasons: list[JsonObject] = field(default_factory=list)


def _text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _payload_amount(payload: dict[str, object]) -> Decimal | None:
    try:
        amount = Decimal(str(payload["amount"]))
    except (KeyError, InvalidOperation):
        return None
    return amount if amount.is_finite() else None


def _block_json(reasons: list[JsonObject]) -> JsonObject | None:
    return {"reasons": sorted(reasons, key=canonical_payload)} if reasons else None


class _Classifier:
    def __init__(self, session: AsyncSession, scope: Scope, symbols: dict[str, _Symbol]) -> None:
        self.session = session
        self.scope = scope
        self.symbols = symbols
        self.scope_reasons: list[JsonObject] = []
        self.provenance: dict[str, set[UUID]] = {}
        self.attempts: dict[UUID, SubmissionAttemptJournalRow] = {}

    def block(self, symbol: str, reason: str, **context: object) -> None:
        """Block ``symbol``; a symbol with no row in this basis blocks the scope."""
        entry: JsonObject = {"reason": reason, **{k: str(v) for k, v in context.items()}}
        values = self.symbols.get(symbol)
        target = values.reasons if values is not None else self.scope_reasons
        if values is None:
            entry["symbol"] = symbol
        if entry not in target:
            target.append(entry)

    async def load_provenance(self, venue_ids: Iterable[str]) -> None:
        """One batch: every attempt of this scope naming any of ``venue_ids``."""
        ids = sorted(set(venue_ids) - set(self.provenance))
        if not ids:
            return
        self.provenance.update(await offer_provenance(self.session, self.scope, ids))
        missing = sorted({a for venue in ids for a in self.provenance[venue]} - set(self.attempts))
        if missing:
            for attempt in await self.session.scalars(
                select(SubmissionAttemptJournalRow).where(
                    SubmissionAttemptJournalRow.attempt_id.in_(missing)
                )
            ):
                self.attempts[attempt.attempt_id] = attempt

    def owner(self, venue_id: str, symbol: str) -> SubmissionAttemptJournalRow | None:
        """The attempt that placed a managed offer; None when foreign.

        Raises ``LookupError`` on contradictory provenance.
        """
        return sole_owner(self.provenance.get(venue_id, set()), self.attempts, self.scope, symbol)

    def require_one_story(self, venue_id: str, symbol: str) -> None:
        """An ended offer named by contradictory provenance blocks its symbol: two
        attempts claiming one venue offer could both be classified as reflected."""
        try:
            self.owner(venue_id, symbol)
        except LookupError:
            self.block(symbol, "credit_provenance_conflict", venue_offer_id=venue_id)


async def write_basis(session: AsyncSession, scope: Scope, observation_id: UUID) -> UUID:
    """Classify the just-accepted observation and write its basis in this transaction.

    Every input is a stored row of the observation, its evidence included (the port's
    ``history_symbols`` and ``unconfirmed_offer_ends``): the basis is recomputable from them.
    """
    observation = await session.get(LedgerObservationRow, observation_id)
    if observation is None or not observation.accepted:
        raise ValueError("basis requires an accepted observation")
    previous = await previous_basis(session, scope)

    wallets = (
        await session.scalars(
            select(LedgerObservationWalletRow).where(
                LedgerObservationWalletRow.observation_id == observation_id
            )
        )
    ).all()
    offers = (
        await session.scalars(
            select(LedgerObservationOfferRow).where(
                LedgerObservationOfferRow.observation_id == observation_id
            )
        )
    ).all()
    credits = (
        await session.scalars(
            select(LedgerObservationCreditRow).where(
                LedgerObservationCreditRow.observation_id == observation_id
            )
        )
    ).all()
    history = (
        await session.scalars(
            select(LedgerObservationOfferHistoryRow).where(
                LedgerObservationOfferHistoryRow.observation_id == observation_id
            )
        )
    ).all()
    credit_history = (
        await session.scalars(
            select(LedgerObservationCreditHistoryRow).where(
                LedgerObservationCreditHistoryRow.observation_id == observation_id
            )
        )
    ).all()
    symbols: dict[str, _Symbol] = {
        wallet.symbol: _Symbol(wallet.available) for wallet in wallets if wallet.symbol is not None
    }
    c = _Classifier(session, scope, symbols)

    # Previous basis evidence: its observation's offers now gone, its credits.
    current_ids = {offer.venue_offer_id for offer in offers}
    prior_offers: list[LedgerObservationOfferRow] = []
    prior_credits: list[AcceptedCapitalBasisCreditRow] = []
    prior_attempts: list[AcceptedCapitalBasisAttemptRow] = []
    if previous is not None:
        prior_offers = list(
            (
                await session.scalars(
                    select(LedgerObservationOfferRow).where(
                        LedgerObservationOfferRow.observation_id == previous.observation_id
                    )
                )
            ).all()
        )
        prior_credits = list(
            (
                await session.scalars(
                    select(AcceptedCapitalBasisCreditRow).where(
                        AcceptedCapitalBasisCreditRow.basis_id == previous.id
                    )
                )
            ).all()
        )
        prior_attempts = list(
            (
                await session.scalars(
                    select(AcceptedCapitalBasisAttemptRow).where(
                        AcceptedCapitalBasisAttemptRow.basis_id == previous.id,
                        AcceptedCapitalBasisAttemptRow.classification == "unresolved",
                    )
                )
            ).all()
        )

    gone = [row for row in prior_offers if row.venue_offer_id not in current_ids]

    trade_rows = list(
        await session.scalars(
            select(LedgerObservationTradeRow).where(
                LedgerObservationTradeRow.observation_id == observation_id
            )
        )
    )
    traded_offer_ids = {trade.venue_offer_id for trade in trade_rows}

    # Considered attempts: recorded after the previous basis, plus its unresolved.
    high_water = previous.attempt_seq_high_water if previous is not None else 0
    considered = list(
        (
            await session.scalars(
                select(SubmissionAttemptJournalRow)
                .where(
                    SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
                    or_(
                        SubmissionAttemptJournalRow.attempt_seq > high_water,
                        SubmissionAttemptJournalRow.attempt_id.in_(
                            [row.attempt_id for row in prior_attempts]
                        ),
                    ),
                )
                .order_by(SubmissionAttemptJournalRow.attempt_seq)
            )
        ).all()
    )
    for attempt in considered:
        c.attempts[attempt.attempt_id] = attempt
    considered_ids = [attempt.attempt_id for attempt in considered]
    outcomes: dict[UUID, TransportOutcomeJournalRow] = {}
    resolutions: dict[UUID, ExecutionResolutionJournalRow] = {}
    if considered_ids:
        for outcome_row in await session.scalars(
            select(TransportOutcomeJournalRow).where(
                TransportOutcomeJournalRow.attempt_id.in_(considered_ids)
            )
        ):
            outcomes[outcome_row.attempt_id] = outcome_row
        for resolution_row in await session.scalars(
            select(ExecutionResolutionJournalRow).where(
                ExecutionResolutionJournalRow.attempt_id.in_(considered_ids)
            )
        ):
            assert resolution_row.attempt_id is not None
            resolutions[resolution_row.attempt_id] = resolution_row

    await c.load_provenance(
        [
            *current_ids,
            *(row.venue_offer_id for row in history),
            *(row.venue_offer_id for row in prior_offers),
            *(row.venue_offer_id for row in trade_rows),
        ]
    )

    # Active offers: managed ones count their remaining amount in offered and cell.
    # Nothing else adds to a cell, so each symbol's cells sum to its ``offered``.
    cells: dict[tuple[str, str], Decimal] = {}
    reflected_by_offer: dict[UUID, str] = {}
    for offer in offers:
        remaining, original = offer.amount_remaining, offer.amount_original
        values = symbols.get(offer.symbol)
        if values is None:
            c.block(offer.symbol, "missing_wallet")
            continue
        if remaining <= ZERO or (original is not None and remaining > original):
            c.block(offer.symbol, "invalid_active_offer", venue_offer_id=offer.venue_offer_id)
            continue
        try:
            managed = c.owner(offer.venue_offer_id, offer.symbol)
        except LookupError:
            c.block(offer.symbol, "offer_provenance_conflict", venue_offer_id=offer.venue_offer_id)
            continue
        if managed is None:
            values.foreign += remaining
            continue
        if original is None or _payload_amount(managed.normalized_payload) != original:
            c.block(offer.symbol, "offer_amount_conflict", venue_offer_id=offer.venue_offer_id)
            continue
        reflected_by_offer[managed.attempt_id] = offer.venue_offer_id
        cell = managed.cell_id
        values.offered += remaining
        cells[(offer.symbol, cell)] = cells.get((offer.symbol, cell), ZERO) + remaining

    # Ended offers: this observation's terminal history (filled or not: ``_reflected``
    # takes either as an attempt's end), the previous basis's offers now gone, and
    # every trade. Each must have one provenance story.
    terminal: dict[str, LedgerObservationOfferHistoryRow] = {}
    for row in history:
        if row.venue_offer_id not in terminal or (
            row.occurred_at_ms > terminal[row.venue_offer_id].occurred_at_ms
        ):
            terminal[row.venue_offer_id] = row
        c.require_one_story(row.venue_offer_id, row.symbol)
    for gone_row in gone:
        c.require_one_story(gone_row.venue_offer_id, gone_row.symbol)
    for trade in trade_rows:
        c.require_one_story(trade.venue_offer_id, trade.symbol)

    # Credits count in their symbol only; no cell holds lent stock.
    for credit in credits:
        values = symbols.get(credit.symbol)
        if values is None:
            c.block(credit.symbol, "missing_wallet")
            continue
        if credit.amount <= ZERO:
            c.block(credit.symbol, "invalid_active_credit", venue_credit_id=credit.venue_credit_id)
        values.credits += credit.amount

    # Attempts since the previous basis (and its unresolved ones).
    classified: dict[UUID, tuple[str, str]] = {}
    converted = (
        {
            row.source_attempt_id: row
            for row in await session.scalars(
                select(QuarantineOpeningRow).where(
                    QuarantineOpeningRow.source_attempt_id.in_(considered_ids)
                )
            )
        }
        if considered_ids
        else {}
    )
    for attempt in considered:
        outcome = outcomes.get(attempt.attempt_id)
        resolution = resolutions.get(attempt.attempt_id)
        venue_id: str | None = None
        if outcome is None:
            c.block(attempt.symbol, "attempt_outcome_missing", attempt_id=attempt.attempt_id)
            classified[attempt.attempt_id] = (attempt.symbol, "unresolved")
            continue
        if outcome.kind in ("rejected", "not_sent"):
            classified[attempt.attempt_id] = (attempt.symbol, "settled")
            continue
        if outcome.kind == "unknown":
            if resolution is None:
                classified[attempt.attempt_id] = (attempt.symbol, "unresolved")
                continue
            if resolution.action == "not_accepted":
                classified[attempt.attempt_id] = (attempt.symbol, "settled")
                continue
            if resolution.action == "bound_to_venue":
                venue_id = resolution.venue_offer_id
        elif outcome.kind == "ack":
            venue_id = outcome.venue_offer_id
        if venue_id is not None and _reflected(
            attempt, venue_id, reflected_by_offer, terminal.get(venue_id), observation
        ):
            classified[attempt.attempt_id] = (attempt.symbol, "reflected")
            continue
        intended_amount = _payload_amount(attempt.normalized_payload)
        source = converted.get(attempt.attempt_id)
        source_matches = source is None or (
            source.exchange_account_id,
            source.symbol,
        ) == (scope.exchange_account_id, attempt.symbol)
        if (
            venue_id is not None
            and source_matches
            and intended_amount is not None
            and intended_amount >= ZERO
            and _can_quarantine(
                attempt.started_at_ms,
                venue_id,
                current_ids,
                terminal.keys(),
                traded_offer_ids,
                observation,
                attempt.symbol,
            )
        ):
            if source is None:
                await open_quarantine(
                    session,
                    scope,
                    Quarantine(
                        uuid4(),
                        attempt.symbol,
                        intended_amount,
                        observation.confirmation_finished_at_ms,
                        {
                            "reason": "ack_unreflected",
                            "venue_offer_id": venue_id,
                            "observation_id": str(observation_id),
                        },
                        source_attempt_id=attempt.attempt_id,
                    ),
                )
            classified[attempt.attempt_id] = (attempt.symbol, "quarantined")
            continue
        # Unaccounted commitment: its symbol stays unresolved and blocks.
        c.block(attempt.symbol, "unclassifiable_commitment", attempt_id=attempt.attempt_id)
        classified[attempt.attempt_id] = (attempt.symbol, "unresolved")
    new_high_water = max([high_water, *(a.attempt_seq for a in considered)])

    quarantines = sorted(
        row.quarantine_id for row in await unresolved_quarantines(session, scope, previous)
    )

    verdicts, end_outcomes = await symbol_verdicts(
        session,
        previous=previous,
        observation=observation,
        symbols=symbols,
        previous_offers=prior_offers,
        previous_credits=prior_credits,
        offers=offers,
        credits=credits,
        credit_history=credit_history,
        terminal=terminal,
        trades=trade_rows,
        provenance=c.provenance,
        quarantines=quarantines,
        unconfirmed_ends=unconfirmed_ends.decode(observation.evidence),
    )
    scope_block = _block_json(c.scope_reasons)
    payload: JsonObject = {
        "domain": "bfx-ledger-capital-basis",
        "version": SCHEMA_VERSION,
        "observation_id": str(observation_id),
        "accept_revision": observation.accept_revision,
        "attempt_seq_high_water": new_high_water,
        "scope_block": scope_block,
        "symbols": [
            [
                name,
                _text(v.available),
                _text(v.offered),
                _text(v.credits),
                _text(v.foreign),
                _block_json(v.reasons),
                verdicts[name].conservation,
                _text(verdicts[name].lent_unexplained),
                _text(verdicts[name].foreign_executed),
                verdicts[name].fill_conflicts,
            ]
            for name, v in sorted(symbols.items())
        ],
        "cells": [[s, cell, _text(amount)] for (s, cell), amount in sorted(cells.items())],
        "credits": [
            [x.source_kind, x.venue_credit_id, x.symbol, _text(x.amount), x.period_days,
             x.mts_opening]
            for x in sorted(credits, key=lambda x: (x.source_kind, x.venue_credit_id))
        ],
        "attempts": [
            [str(attempt_id), symbol, kind]
            for attempt_id, (symbol, kind) in sorted(classified.items(), key=lambda i: str(i[0]))
        ],
        "quarantines": [str(q) for q in quarantines],
    }
    basis_id = uuid4()
    session.add(
        AcceptedCapitalBasisRow(
            id=basis_id,
            exchange_account_id=scope.exchange_account_id,
            observation_id=observation_id,
            accepted=True,
            accept_revision=observation.accept_revision,
            attempt_seq_high_water=new_high_water,
            scope_block=scope_block,
            schema_version=SCHEMA_VERSION,
            digest=sha256(canonical_payload(payload)).hexdigest(),
            accepted_at_ms=observation.confirmation_finished_at_ms,
        )
    )
    await session.flush()
    for name, v in symbols.items():
        session.add(
            AcceptedCapitalBasisSymbolRow(
                basis_id=basis_id,
                symbol=name,
                available=v.available,
                offered=v.offered,
                credits=v.credits,
                # Transitional until the column is dropped (plan step 3): every credit.
                unattributed_credits=v.credits,
                foreign_offers=v.foreign,
                block=_block_json(v.reasons),
                conservation=verdicts[name].conservation,
                lent_unexplained=verdicts[name].lent_unexplained,
                foreign_executed=verdicts[name].foreign_executed,
                fill_conflicts=verdicts[name].fill_conflicts,
            )
        )
    for (name, cell), amount in cells.items():
        session.add(
            AcceptedCapitalBasisCellRow(basis_id=basis_id, symbol=name, cell_id=cell, amount=amount)
        )
    for x in credits:
        session.add(
            AcceptedCapitalBasisCreditRow(
                basis_id=basis_id,
                source_kind=x.source_kind,
                venue_credit_id=x.venue_credit_id,
                symbol=x.symbol,
                amount=x.amount,
                period_days=x.period_days,
                mts_opening=x.mts_opening,
                # Transitional until the column is dropped (plan step 3).
                attribution_basis="unattributed",
            )
        )
    for attempt_id, (symbol, kind) in classified.items():
        session.add(
            AcceptedCapitalBasisAttemptRow(
                basis_id=basis_id, attempt_id=attempt_id, symbol=symbol, classification=kind
            )
        )
    for quarantine_id in quarantines:
        session.add(
            AcceptedCapitalBasisQuarantineRow(basis_id=basis_id, quarantine_id=quarantine_id)
        )
    await session.flush()
    _report_end_outcomes(end_outcomes, verdicts, observation_id)
    return basis_id


def _report_end_outcomes(
    outcomes: list[OfferEndOutcome], verdicts: dict[str, ConservationVerdict], observation_id: UUID,
) -> None:
    """After the verdict, so the signal says what became of each unconfirmed end (the
    re-evaluation trigger "trades cannot decide, repeatedly" is this count)."""
    for item in outcomes:
        verdict = verdicts[item.symbol].conservation if item.symbol in verdicts else "unjudged"
        log.warning(
            "offer_end_judged offer=%s symbol=%s outcome=%s filled=%s verdict=%s observation=%s",
            item.venue_offer_id, item.symbol, item.outcome, item.filled, verdict, observation_id,
        )
        alerts.emit(
            alerts.OFFER_END_JUDGED,
            level=alerts.INFO if item.outcome == "explained" else alerts.WARNING,
            symbol=item.symbol, venue_offer_id=item.venue_offer_id, outcome=item.outcome,
            filled=None if item.filled is None else str(item.filled), verdict=verdict,
        )


def _trades_cover(anchor_ms: int, observation: LedgerObservationRow) -> bool:
    """Requested trades range, not row timestamps, must cover the anchor and margin."""
    start, end = observation.trades_requested_start_ms, observation.trades_requested_end_ms
    return (
        observation.trades_complete
        and start is not None
        and end is not None
        and start <= anchor_ms - HISTORY_QUERY_MARGIN_MS
        and end >= anchor_ms
    )


def _can_quarantine(
    started_at_ms: int,
    venue_id: str,
    active_ids: Iterable[str],
    terminal_ids: Iterable[str],
    trade_ids: Iterable[str],
    observation: LedgerObservationRow,
    symbol: str,
) -> bool:
    """R6 absence is proven only by complete evidence spanning the local start,
    for a symbol whose history (and trades) the port declared fetching."""
    start, end = observation.history_requested_start_ms, observation.history_requested_end_ms
    return (
        history_symbols.declared(observation.evidence, symbol)
        and observation.offer_history_complete
        and _trades_cover(started_at_ms, observation)
        and start is not None
        and end is not None
        and start <= started_at_ms - HISTORY_QUERY_MARGIN_MS
        and end >= started_at_ms
        and venue_id not in active_ids
        and venue_id not in terminal_ids
        and venue_id not in trade_ids
    )


def _reflected(
    attempt: SubmissionAttemptJournalRow,
    venue_id: str,
    reflected_by_offer: dict[UUID, str],
    terminal: LedgerObservationOfferHistoryRow | None,
    observation: LedgerObservationRow,
) -> bool:
    """Exact venue identity: an active managed offer, or its terminal history row."""
    if reflected_by_offer.get(attempt.attempt_id) == venue_id:
        return True
    start, end = observation.history_requested_start_ms, observation.history_requested_end_ms
    return (
        terminal is not None
        and observation.offer_history_complete
        and start is not None
        and end is not None
        and start <= attempt.started_at_ms  # both local: requested range, attempt start
        # A venue stamp against the locally requested end: allow venue clock skew.
        and terminal.occurred_at_ms <= end + VENUE_CLOCK_TOLERANCE_MS
        and terminal.symbol == attempt.symbol
        and terminal.amount_original is not None
        and terminal.amount_original == _payload_amount(attempt.normalized_payload)
    )


__all__ = [
    "write_basis",
]
