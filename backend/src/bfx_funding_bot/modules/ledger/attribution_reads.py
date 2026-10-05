"""Read port for the weekly attribution: offer -> cell and credits, from the ledger.

After the authority switch new offers exist only in the submission journal (and its
transport outcomes) and open credits only in ``venue_credit_mirror``; the legacy
``offer_claims`` and venue-state projections stop growing. The weekly attribution
(``live_validation``) unions these reads with the legacy sources, so it needs them through
the ledger's public surface (``_internal`` is private). Plain reads on the caller's session
under the bot role's table-level SELECT; nothing is written, nothing is inferred.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger._internal.provenance import offer_provenance
from bfx_funding_bot.modules.ledger.tables import (
    SubmissionAttemptJournalRow,
    VenueCreditMirrorRow,
)

_BATCH = 2000


@dataclass(frozen=True, slots=True)
class JournalOfferCell:
    """A ledger attempt that placed a venue offer (ack or bound_to_venue) and its cell.

    ``seeded`` marks an attempt the legacy -> ledger seed wrote from a legacy attempt or
    claim; the same offer is then also in the legacy tables and both must name one cell.
    """

    venue_offer_id: str
    cell_id: str
    execution_decision_id: str
    seeded: bool
    attempt_id: UUID | None = None


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
    mts_updated: int | None
    terminal: bool  # ended: a terminal evidence row closed it (``mts_updated`` = its end)


async def journal_offer_cells(
    session: AsyncSession, scope: Scope, venue_offer_ids: Collection[str]
) -> tuple[JournalOfferCell, ...]:
    """The attempts that placed each of ``venue_offer_ids``, by offer id then attempt.

    Provenance is the ledger's own (``offer_provenance``): a transport ``ack`` outcome or a
    ``bound_to_venue`` resolution (resolver auto-bind or operator) names the offer. An offer
    no attempt names is absent (foreign to this source). One row per (offer, attempt): the
    caller treats several attempts of one offer as one cell when they agree and as a conflict
    when they differ."""
    ids = sorted(set(venue_offer_ids))
    owners: dict[str, set[UUID]] = {}
    for start in range(0, len(ids), _BATCH):
        for offer, attempts in (
            await offer_provenance(session, scope, ids[start:start + _BATCH])
        ).items():
            if attempts:
                owners[offer] = attempts
    wanted = sorted({a for attempts in owners.values() for a in attempts})
    attempts_by_id: dict[UUID, tuple[str, str, bool]] = {}
    for start in range(0, len(wanted), _BATCH):
        rows = await session.execute(
            select(
                SubmissionAttemptJournalRow.attempt_id,
                SubmissionAttemptJournalRow.cell_id,
                SubmissionAttemptJournalRow.execution_decision_id,
                SubmissionAttemptJournalRow.seed_provenance.is_not(None),
            ).where(
                SubmissionAttemptJournalRow.attempt_id.in_(wanted[start:start + _BATCH]),
                SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
                SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
            )
        )
        for attempt_id, cell, decision, seeded in rows.tuples():
            attempts_by_id[attempt_id] = (cell, decision, bool(seeded))
    return tuple(
        JournalOfferCell(offer, *attempts_by_id[attempt], attempt_id=attempt)
        for offer in sorted(owners)
        for attempt in sorted(owners[offer], key=str)
        if attempt in attempts_by_id
    )


async def mirror_credits(session: AsyncSession, scope: Scope) -> tuple[MirrorCredit, ...]:
    """Every credit and loan the ledger mirror knows for the scope (open and ended)."""
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
            VenueCreditMirrorRow.mts_updated,
            VenueCreditMirrorRow.terminal_kind.is_not(None),
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
            None if rate is None else Decimal(rate), period_days, created, opening, updated,
            terminal=bool(terminal),
        )
        for credit_id, kind, symbol, amount, rate, period_days, created, opening, updated,
        terminal in rows.tuples()
    )
