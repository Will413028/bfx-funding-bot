"""Classify an accepted observation into the capital basis (venue facts only).

Ports legacy ``CapitalRepository._classify`` / ``_attribute_credits`` onto the
journals and the observation store. Policy is not read here: it is applied per
symbol when the basis is read. Every query is bounded by the current
observation, the previous basis, or the attempts recorded after the previous
basis's ``attempt_seq_high_water``.

Provenance of a venue offer (``_internal.provenance``): a transport ``ack``
outcome or a ``bound_to_venue`` resolution names it -> the attempt. No
provenance means the offer is foreign; more than one story is a fact-level
block, never a reason to call it foreign.

Blocks are per symbol (``accepted_capital_basis_symbol.block``); a fact that
cannot be tied to a symbol row of this basis goes to ``scope_block``. Each symbol
row also stores its conservation verdict against the previous accepted basis
(``ledger/conservation.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from bfx_funding_bot.core.venue_time import HISTORY_QUERY_MARGIN_MS, VENUE_CLOCK_TOLERANCE_MS
from bfx_funding_bot.modules.ledger import JsonObject, Quarantine, Scope
from bfx_funding_bot.modules.ledger._internal import history_symbols
from bfx_funding_bot.modules.ledger._internal.conservation_facts import symbol_verdicts
from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload
from bfx_funding_bot.modules.ledger._internal.provenance import offer_provenance, sole_owner
from bfx_funding_bot.modules.ledger._internal.quarantine import (
    open_quarantine,
    unresolved_quarantines,
)
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisCellRow,
    AcceptedCapitalBasisCreditCellRow,
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisQuarantineRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    ExecutionResolutionJournalRow,
    LedgerObservationCreditHistoryRow,
    LedgerObservationCreditRow,
    LedgerObservationOfferHistoryRow,
    LedgerObservationOfferRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
    LedgerObservationTradeRow,
    LedgerObservationWalletRow,
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)

SCHEMA_VERSION = 1
ZERO = Decimal(0)

type Group = tuple[str, int, int]
type CreditKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _Fill:
    cell: str
    symbol: str
    filled: Decimal
    created: int | None


@dataclass(slots=True)
class _Symbol:
    available: Decimal
    offered: Decimal = ZERO
    credits: Decimal = ZERO
    unattributed: Decimal = ZERO
    foreign: Decimal = ZERO
    reasons: list[JsonObject] = field(default_factory=list)


@dataclass(slots=True)
class _Credit:
    source_kind: str
    venue_credit_id: str
    symbol: str
    amount: Decimal
    period_days: int | None
    opening: int | None
    attribution: str
    cells: set[str] = field(default_factory=set)


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

    def cell_of(self, venue_id: str, symbol: str) -> str | None:
        """The owning cell for fill evidence; None when foreign or contradictory."""
        try:
            attempt = self.owner(venue_id, symbol)
        except LookupError:
            self.block(symbol, "credit_provenance_conflict", venue_offer_id=venue_id)
            return None
        return attempt.cell_id if attempt is not None else None


async def previous_basis(session: AsyncSession, scope: Scope) -> AcceptedCapitalBasisRow | None:
    """The latest accepted basis of the scope (by its query revision).

    The query's own scope columns are repeated (the basis and observation insert
    triggers already make them equal to the basis's) so the planner can walk
    ``ix_ledger_observation_query_scope_revision`` newest-first and stop at the
    first query with a basis, instead of sorting every basis of the scope.
    """
    row: AcceptedCapitalBasisRow | None = await session.scalar(
        select(AcceptedCapitalBasisRow)
        .join(
            LedgerObservationRow, LedgerObservationRow.id == AcceptedCapitalBasisRow.observation_id
        )
        .join(
            LedgerObservationQueryRow,
            LedgerObservationQueryRow.query_id == LedgerObservationRow.query_id,
        )
        .where(
            LedgerObservationQueryRow.exchange_account_id == scope.exchange_account_id,
            LedgerObservationQueryRow.deployment_environment == scope.deployment_environment,
            AcceptedCapitalBasisRow.exchange_account_id == scope.exchange_account_id,
            AcceptedCapitalBasisRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(LedgerObservationQueryRow.query_revision.desc())
        .limit(1)
        .options(
            load_only(
                AcceptedCapitalBasisRow.id,
                AcceptedCapitalBasisRow.observation_id,
                AcceptedCapitalBasisRow.accept_revision,
                AcceptedCapitalBasisRow.attempt_seq_high_water,
            )
        )
    )
    return row


async def write_basis(
    session: AsyncSession, scope: Scope, observation_id: UUID,
    *, unconfirmed_ends: frozenset[str] = frozenset(),
) -> UUID:
    """Classify the just-accepted observation and write its basis in this transaction.

    ``unconfirmed_ends`` are the observation's offers the venue could not date an end for
    (see ``Observation.unconfirmed_ends``); only conservation reads them.
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
    prior_cells: dict[CreditKey, set[str]] = {}
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
        for cell_row in await session.scalars(
            select(AcceptedCapitalBasisCreditCellRow).where(
                AcceptedCapitalBasisCreditCellRow.basis_id == previous.id
            )
        ):
            prior_cells.setdefault((cell_row.source_kind, cell_row.venue_credit_id), set()).add(
                cell_row.cell_id
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

    # Credit groups (symbol, period, opening) and this observation's trades at them.
    group_of: dict[CreditKey, Group] = {}
    live: dict[Group, Decimal] = {}
    for credit in credits:
        if credit.period_days is not None and credit.mts_opening is not None:
            key: Group = (credit.symbol, credit.period_days, credit.mts_opening)
            group_of[(credit.source_kind, credit.venue_credit_id)] = key
            live[key] = live.get(key, ZERO) + credit.amount
    trades: list[tuple[Group, Decimal, str]] = []
    traded_offer_ids: set[str] = set()
    trade_rows = list(
        await session.scalars(
            select(LedgerObservationTradeRow).where(
                LedgerObservationTradeRow.observation_id == observation_id
            )
        )
    )
    for trade in trade_rows:
        traded_offer_ids.add(trade.venue_offer_id)
        key = (trade.symbol, trade.period_days, trade.mts_create)
        if key in live:
            trades.append((key, trade.amount, trade.venue_offer_id))

    # Considered attempts: recorded after the previous basis, plus its unresolved.
    high_water = previous.attempt_seq_high_water if previous is not None else 0
    considered = list(
        (
            await session.scalars(
                select(SubmissionAttemptJournalRow)
                .where(
                    SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
                    SubmissionAttemptJournalRow.deployment_environment
                    == scope.deployment_environment,
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
            *(offer_id for _, _, offer_id in trades),
        ]
    )

    # Active offers: managed ones count their remaining amount in offered and cell.
    cells: dict[tuple[str, str], Decimal] = {}
    fills: list[_Fill] = []
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
        if original - remaining > ZERO:
            fills.append(_Fill(cell, offer.symbol, original - remaining, offer.mts_created))

    # Fill evidence: this observation's terminal history, previous managed offers gone.
    terminal: dict[str, LedgerObservationOfferHistoryRow] = {}
    for row in history:
        if row.venue_offer_id not in terminal or (
            row.occurred_at_ms > terminal[row.venue_offer_id].occurred_at_ms
        ):
            terminal[row.venue_offer_id] = row
        filled = (row.amount_original or ZERO) - row.amount_remaining
        if filled > ZERO:
            owner_cell = c.cell_of(row.venue_offer_id, row.symbol)
            if owner_cell is not None:
                fills.append(_Fill(owner_cell, row.symbol, filled, row.mts_created))
    for gone_row in gone:
        owner_cell = c.cell_of(gone_row.venue_offer_id, gone_row.symbol)
        if owner_cell is not None:
            fills.append(
                _Fill(owner_cell, gone_row.symbol, gone_row.amount_remaining, gone_row.mts_created)
            )

    # Credits: trade, carry, recent fill; otherwise unattributed (U).
    traders: dict[Group, set[str]] = {}
    traded: dict[Group, Decimal] = {}
    for key, amount, offer_id in trades:
        traded[key] = traded.get(key, ZERO) + amount
        owners = traders.setdefault(key, set())
        owner_cell = c.cell_of(offer_id, key[0])
        if owner_cell is not None:
            owners.add(owner_cell)
    carried: dict[Group, set[str]] = {}
    for prior in prior_credits:
        if prior.period_days is not None and prior.mts_opening is not None:
            carried.setdefault((prior.symbol, prior.period_days, prior.mts_opening), set()).update(
                prior_cells.get((prior.source_kind, prior.venue_credit_id), set())
            )
    attributed: list[_Credit] = []
    for credit in credits:
        group = group_of.get((credit.source_kind, credit.venue_credit_id))
        # A group carried from the previous basis is already attributed; only a
        # new opening needs trades evidence covering it (else old credits would
        # demand trades back to their opening on every observation).
        if (group is None or group not in carried) and (
            credit.mts_opening is None or not _trades_cover(credit.mts_opening, observation)
        ):
            c.block(
                credit.symbol,
                "trades_range_uncovered",
                venue_credit_id=credit.venue_credit_id,
                source_kind=credit.source_kind,
            )
        amount = credit.amount
        values = symbols.get(credit.symbol)
        if values is None:
            c.block(credit.symbol, "missing_wallet")
        elif amount <= ZERO:
            c.block(credit.symbol, "invalid_active_credit", venue_credit_id=credit.venue_credit_id)
        owners_now: set[str]
        if group is not None and group in traded and live[group] <= traded[group]:
            owners_now, basis = set(traders[group]), "trade"
        else:
            if group is not None and group in carried:
                owners_now, basis = set(carried[group]), "carry"
            else:
                owners_now = {
                    fill.cell
                    for fill in fills
                    if fill.symbol == credit.symbol
                    and amount <= fill.filled
                    and (
                        fill.created is None
                        or credit.mts_opening is None
                        or fill.created <= credit.mts_opening
                    )
                }
                basis = "recent_fill" if owners_now else "unattributed"
            if group is not None and group in traders:
                # More live than traded: each record keeps every candidate cell.
                owners_now |= traders[group]
                basis = "trade"
        attributed.append(
            _Credit(
                credit.source_kind,
                credit.venue_credit_id,
                credit.symbol,
                amount,
                credit.period_days,
                credit.mts_opening,
                basis,
                owners_now,
            )
        )
        if values is None:
            continue
        values.credits += amount
        if not owners_now:
            values.unattributed += amount
        for owner_cell in owners_now:
            cells[(credit.symbol, owner_cell)] = (
                cells.get((credit.symbol, owner_cell), ZERO) + amount
            )

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
            source.deployment_environment,
            source.symbol,
        ) == (scope.exchange_account_id, scope.deployment_environment, attempt.symbol)
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

    verdicts = await symbol_verdicts(
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
        unconfirmed_ends=unconfirmed_ends,
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
                _text(v.unattributed),
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
            [
                x.source_kind,
                x.venue_credit_id,
                x.symbol,
                _text(x.amount),
                x.period_days,
                x.opening,
                x.attribution,
                sorted(x.cells),
            ]
            for x in sorted(attributed, key=lambda x: (x.source_kind, x.venue_credit_id))
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
            deployment_environment=scope.deployment_environment,
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
                unattributed_credits=v.unattributed,
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
    for x in attributed:
        session.add(
            AcceptedCapitalBasisCreditRow(
                basis_id=basis_id,
                source_kind=x.source_kind,
                venue_credit_id=x.venue_credit_id,
                symbol=x.symbol,
                amount=x.amount,
                period_days=x.period_days,
                mts_opening=x.opening,
                attribution_basis=x.attribution,
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
    for x in attributed:
        for cell in sorted(x.cells):
            session.add(
                AcceptedCapitalBasisCreditCellRow(
                    basis_id=basis_id,
                    source_kind=x.source_kind,
                    venue_credit_id=x.venue_credit_id,
                    cell_id=cell,
                )
            )
    await session.flush()
    return basis_id


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


__all__ = ["previous_basis", "write_basis"]
