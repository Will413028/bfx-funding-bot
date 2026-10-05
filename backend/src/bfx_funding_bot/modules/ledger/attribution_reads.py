"""Read port for the weekly attribution: offer -> cell and credits, from the ledger.

After the authority switch new offers exist only in the submission journal (and its
transport outcomes) and open credits only in ``venue_credit_mirror``; the legacy
``offer_claims`` and venue-state projections stop growing. The weekly attribution
(``live_validation``) unions these reads with the legacy sources, so it needs them through
the ledger's public surface (``_internal`` is private). Plain reads on the caller's session
under the bot role's table-level SELECT; nothing is written, nothing is inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import (
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
    VenueCreditMirrorRow,
)


@dataclass(frozen=True, slots=True)
class JournalOfferCell:
    """An acknowledged ledger attempt: the venue offer it created and its cell.

    ``seeded`` marks an attempt the legacy -> ledger seed wrote from a legacy attempt or
    claim; the same offer is then also in the legacy tables and both must name one cell.
    """

    venue_offer_id: str
    cell_id: str
    execution_decision_id: str
    seeded: bool


@dataclass(frozen=True, slots=True)
class MirrorCredit:
    """One credit or loan as the accepted venue observations last left it."""

    venue_credit_id: str
    source_kind: str  # "credit" | "loan"
    symbol: str
    amount: Decimal
    rate: Decimal | None
    period_days: int | None
    mts_created: int | None
    mts_opening: int | None
    open: bool  # live in the latest accepted snapshot and not terminal


async def journal_offer_cells(
    session: AsyncSession, scope: Scope
) -> tuple[JournalOfferCell, ...]:
    """Every acknowledged attempt of the scope with its venue offer id, by offer id."""
    rows = await session.execute(
        select(
            TransportOutcomeJournalRow.venue_offer_id,
            SubmissionAttemptJournalRow.cell_id,
            SubmissionAttemptJournalRow.execution_decision_id,
            SubmissionAttemptJournalRow.seed_provenance.is_not(None),
        )
        .join(
            TransportOutcomeJournalRow,
            TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
        )
        .where(
            SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
            SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
            TransportOutcomeJournalRow.kind == "ack",
            TransportOutcomeJournalRow.venue_offer_id.is_not(None),
        )
        .order_by(TransportOutcomeJournalRow.venue_offer_id, SubmissionAttemptJournalRow.attempt_seq)
    )
    return tuple(
        JournalOfferCell(str(offer), cell, decision, bool(seeded))
        for offer, cell, decision, seeded in rows.tuples()
    )


async def mirror_credits(session: AsyncSession, scope: Scope) -> tuple[MirrorCredit, ...]:
    """Every credit and loan the ledger mirror knows for the scope (open and terminal)."""
    rows = await session.execute(
        select(
            VenueCreditMirrorRow.venue_credit_id,
            VenueCreditMirrorRow.source_kind,
            VenueCreditMirrorRow.symbol,
            VenueCreditMirrorRow.amount,
            VenueCreditMirrorRow.rate,
            VenueCreditMirrorRow.period_days,
            VenueCreditMirrorRow.mts_created,
            VenueCreditMirrorRow.mts_opening,
            VenueCreditMirrorRow.present_in_latest_accepted_snapshot,
            VenueCreditMirrorRow.terminal_kind.is_(None),
        )
        .where(
            VenueCreditMirrorRow.exchange_account_id == scope.exchange_account_id,
            VenueCreditMirrorRow.deployment_environment == scope.deployment_environment,
        )
        .order_by(VenueCreditMirrorRow.source_kind, VenueCreditMirrorRow.venue_credit_id)
    )
    return tuple(
        MirrorCredit(
            str(credit_id), kind, symbol, Decimal(amount),
            None if rate is None else Decimal(rate), period_days, created, opening,
            open=bool(present) and bool(not_terminal),
        )
        for credit_id, kind, symbol, amount, rate, period_days, created, opening, present,
        not_terminal in rows.tuples()
    )
