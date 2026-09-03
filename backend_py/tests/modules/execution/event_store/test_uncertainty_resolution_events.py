"""Event-only operator resolution projection and clean replay contracts."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.execution.events import (
    ReservationIntent,
    ReservationUnknown,
    SnapshotCoverage,
    UncertaintyBoundToVenueOffer,
    UncertaintyManuallyResolved,
    UncertaintyMarkedNotAccepted,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")
ENV = "ci"
SCID = UUID("11111111-1111-1111-1111-111111111111")


def _coverage(*, history: bool = True) -> SnapshotCoverage:
    return SnapshotCoverage(
        active_offers_complete=True,
        active_credits_complete=True,
        wallets_complete=True,
        offer_history_complete=history,
        offer_history_pages=1 if history else 0,
        offer_history_start_ms=1 if history else None,
        offer_history_end_ms=10_000 if history else None,
    )


def _snapshot(*, finished: int, offer: bool = False, history: bool = True) -> VenueSnapshotObserved:
    values = (
        VenueOfferObservation(
            venue_offer_id="venue-1",
            symbol="fUST",
            amount_original=Decimal("100"),
            amount_remaining=Decimal("100"),
            rate=Decimal("0.001"),
            period_days=2,
            status="active",
            mts_created=finished,
            mts_updated=finished,
        ),
    ) if offer else ()
    return VenueSnapshotObserved(
        account_id=str(ACCOUNT),
        environment=ENV,
        query_started_at_ms=finished - 1,
        query_finished_at_ms=finished,
        offers=values,
        credits=(),
        wallet_available={"fUST": Decimal("500")},
        coverage=_coverage(history=history),
    )


def _decision() -> ExecutionDecisionRow:
    return ExecutionDecisionRow(
        decision_id="decision-1",
        account_id=str(ACCOUNT),
        exchange_account_id=ACCOUNT,
        deployment_environment=ENV,
        reconcile_id="reconcile-1",
        cell_id="cell-1",
        symbol="fUST",
        signal_correlation_id=str(SCID),
        outcome="ready",
        signal_rate=Decimal("0.001"),
        applied_rate=Decimal("0.001"),
        amount_usdt=Decimal("100"),
        duration_days=2,
        model_evidence={},
        safety_result={},
        execution_policy="standard",
        service_version="test",
        config_hash="test",
        occurred_at_ms=1,
        recorded_at_ms=1,
    )


def _attempt() -> SubmissionAttemptPayload:
    return SubmissionAttemptPayload(
        execution_decision_id="decision-1",
        account_id=ACCOUNT,
        environment=ENV,
        symbol="fUST",
        cid=7,
        normalized_payload={
            "type": "LIMIT",
            "symbol": "fUST",
            "amount": "100",
            "rate": "0.001",
            "period": 2,
        },
        started_at_ms=1,
    )


async def _seed_base(session: AsyncSession) -> None:
    session.add(ExchangeAccount(id=ACCOUNT, venue="bitfinex", label="primary"))
    session.add(_decision())
    await session.flush()


@pytest_asyncio.fixture
async def factory(sqlite_engine) -> async_sessionmaker[AsyncSession]:
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


async def _open_unknown(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as session:
        await _seed_base(session)
        store = PostgresEventStore(deployment_environment=ENV)
        await store.append(session, _snapshot(finished=1))
        reference = ReservationRef(
            execution_decision_id="decision-1",
            cid=7,
            signal_correlation_id=SCID,
        )
        await AccountEventWriter(store=store).append(
            session,
            ReservationIntent(
                symbol="fUST",
                cid=7,
                signal_correlation_id=SCID,
                account_id=str(ACCOUNT),
                is_simulated=True,
                execution_decision_id="decision-1",
                reservation_ref=reference,
                submission_attempt=_attempt(),
                amount=Decimal("100"),
                occurred_at_ms=2,
            ),
        )
        await store.append(
            session,
            ReservationUnknown(
                symbol="fUST",
                cid=7,
                signal_correlation_id=SCID,
                account_id=str(ACCOUNT),
                is_simulated=True,
                reason="connection_reset",
                reservation_ref=reference,
                amount=Decimal("100"),
                occurred_at_ms=3,
            ),
        )
        await session.commit()
    async with factory() as session:
        return await session.scalar(
            select(ExecutionUncertaintyRow.uncertainty_id).where(
                ExecutionUncertaintyRow.exchange_account_id == ACCOUNT,
                ExecutionUncertaintyRow.state == "open",
            )
        )


@pytest.mark.asyncio
async def test_bind_resolution_projects_attempt_claim_and_replays(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    uncertainty_id = await _open_unknown(factory)
    async with factory() as session:
        store = PostgresEventStore(deployment_environment=ENV)
        reconcile = await store.append_snapshot(
            session, _snapshot(finished=4, offer=True)
        )
        del reconcile
        latest = await session.scalar(
            select(EventLogRow.event_seq)
            .where(EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED")
            .order_by(EventLogRow.event_seq.desc())
            .limit(1)
        )
        assert latest is not None
        result = await AccountEventWriter(store=store).append(
            session,
            UncertaintyBoundToVenueOffer(
                uncertainty_id=uncertainty_id,
                account_id=str(ACCOUNT),
                environment=ENV,
                symbol="fUST",
                kind="submit_outcome_unknown",
                venue_offer_id="venue-1",
                reconcile_event_seq=latest,
                resolved_by_operator_id="operator-1",
                resolution_reason="confirmed exact venue offer",
                resolution_evidence={"candidate_count": 1},
                occurred_at_ms=5,
            ),
        )
        await session.commit()
        assert result.event_seq > latest

    async with factory() as session:
        row = await session.get(ExecutionUncertaintyRow, uncertainty_id)
        attempt = await session.scalar(select(SubmissionAttemptRow))
        claim = await session.scalar(select(OfferClaimRow).where(OfferClaimRow.cid == 7))
        assert row is not None and row.state == "resolved"
        assert attempt is not None and attempt.venue_offer_id == "venue-1"
        assert claim is not None and claim.venue_offer_id == "venue-1"

    # A later reconcile must not make the earlier resolution fail during a
    # clean replay: the event's fence is evaluated against snapshots that were
    # visible before that resolution, not against the stream's eventual tail.
    async with factory() as session:
        await PostgresEventStore(deployment_environment=ENV).append_snapshot(
            session, _snapshot(finished=6, offer=True)
        )
        await session.commit()

    async with factory() as session:
        await store_rebuild(session)

    async with factory() as session:
        row = await session.get(ExecutionUncertaintyRow, uncertainty_id)
        attempt_row = await session.scalar(select(SubmissionAttemptRow))
        assert row is not None and row.state == "resolved"
        assert attempt_row is not None and attempt_row.venue_offer_id == "venue-1"


async def store_rebuild(session: AsyncSession) -> None:
    await PostgresEventStore(deployment_environment=ENV).rebuild_snapshot_from_log(
        session,
        account_id=str(ACCOUNT),
        deployment_environment=ENV,
    )
    await session.flush()


@pytest.mark.asyncio
async def test_mark_not_accepted_requires_zero_candidates_and_replays(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    uncertainty_id = await _open_unknown(factory)
    async with factory() as session:
        store = PostgresEventStore(deployment_environment=ENV)
        await store.append_snapshot(session, _snapshot(finished=4, offer=False))
        latest = await session.scalar(
            select(EventLogRow.event_seq)
            .where(EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED")
            .order_by(EventLogRow.event_seq.desc())
            .limit(1)
        )
        assert latest is not None
        await AccountEventWriter(store=store).append(
            session,
            UncertaintyMarkedNotAccepted(
                uncertainty_id=uncertainty_id,
                account_id=str(ACCOUNT),
                environment=ENV,
                symbol="fUST",
                kind="submit_outcome_unknown",
                reconcile_event_seq=latest,
                resolved_by_operator_id="operator-1",
                resolution_reason="complete history had zero candidates",
                resolution_evidence={"candidate_count": 0},
                candidate_count=0,
                occurred_at_ms=5,
            ),
        )
        await session.commit()
    async with factory() as session:
        row = await session.get(ExecutionUncertaintyRow, uncertainty_id)
        assert row is not None and row.state == "resolved"
        await store_rebuild(session)
    async with factory() as session:
        row = await session.get(ExecutionUncertaintyRow, uncertainty_id)
        assert row is not None and row.state == "resolved"


@pytest.mark.asyncio
async def test_manual_resolution_is_event_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await _seed_base(session)
        store = PostgresEventStore(deployment_environment=ENV)
        await store.append(session, _snapshot(finished=1, offer=True))
        await store.append(
            session,
            VenueOfferQuarantined(
                venue_offer_id="venue-1",
                symbol="fUST",
                amount=Decimal("100"),
                account_id=str(ACCOUNT),
                observed_at_ms=2,
            ),
        )
        await session.commit()
    async with factory() as session:
        uncertainty_id = await session.scalar(
            select(ExecutionUncertaintyRow.uncertainty_id).where(
                ExecutionUncertaintyRow.kind == "unattributed_venue_offer",
                ExecutionUncertaintyRow.state == "open",
            )
        )
        store = PostgresEventStore(deployment_environment=ENV)
        await store.append(session, _snapshot(finished=4, offer=True))
        latest = await session.scalar(
            select(EventLogRow.event_seq)
            .where(EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED")
            .order_by(EventLogRow.event_seq.desc())
            .limit(1)
        )
        assert uncertainty_id is not None and latest is not None
        await AccountEventWriter(store=store).append(
            session,
            UncertaintyManuallyResolved(
                uncertainty_id=uncertainty_id,
                account_id=str(ACCOUNT),
                environment=ENV,
                symbol="fUST",
                kind="unattributed_venue_offer",
                reconcile_event_seq=latest,
                resolved_by_operator_id="operator-1",
                resolution_reason="operator accepted the quarantined offer",
                resolution_evidence={"decision": "accept"},
                occurred_at_ms=5,
            ),
        )
        await session.commit()
    async with factory() as session:
        row = await session.get(ExecutionUncertaintyRow, uncertainty_id)
        assert row is not None and row.state == "resolved"
        assert (await session.scalar(select(EventLogRow).where(
            EventLogRow.event_type == "UNCERTAINTY_MANUALLY_RESOLVED",
        ))) is not None
