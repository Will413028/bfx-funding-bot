"""Account-scoped uncertainty API contract tests.

These tests intentionally exercise the public router through FastAPI and a real
SQLite projection.  Venue resolution is represented by the event writer; the
router must not call the legacy ``UncertaintyService.resolve`` mutation path.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.execution.uncertainty_tables  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import (
    ReservationIntent,
    ReservationUnknown,
    SnapshotCoverage,
    VenueOfferObservation,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow

ACCOUNT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
OTHER_ACCOUNT_ID = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
SCID = UUID("11111111-1111-1111-1111-111111111111")


async def _seed_account(session, account_id: UUID, user_id: str) -> None:
    session.add(
        ExchangeAccount(
            id=account_id,
            venue="bitfinex",
            label=str(account_id),
            lifecycle_status="active",
        )
    )
    await session.flush()
    await grant_membership(
        session,
        exchange_account_id=account_id,
        user_id=user_id,
        role="owner",
    )


def _decision() -> ExecutionDecisionRow:
    return ExecutionDecisionRow(
        decision_id="decision-7",
        account_id=str(ACCOUNT_ID),
        exchange_account_id=ACCOUNT_ID,
        deployment_environment="ci",
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
        occurred_at_ms=900,
        recorded_at_ms=900,
    )


def _attempt() -> SubmissionAttemptPayload:
    return SubmissionAttemptPayload(
        execution_decision_id="decision-7",
        account_id=ACCOUNT_ID,
        environment="ci",
        symbol="fUST",
        cid=7,
        normalized_payload={
            "type": "LIMIT",
            "symbol": "fUST",
            "amount": "100",
            "rate": "0.001",
            "period": 2,
        },
        started_at_ms=1000,
    )


def _snapshot(
    account_id: UUID,
    *,
    finished_at: int,
    offers: tuple[VenueOfferObservation, ...] = (),
    history_complete: bool = True,
) -> VenueSnapshotObserved:
    return VenueSnapshotObserved(
        account_id=str(account_id),
        environment="ci",
        query_started_at_ms=finished_at - 10,
        query_finished_at_ms=finished_at,
        offers=offers,
        credits=(),
        wallet_available={"fUST": Decimal("500")},
        coverage=SnapshotCoverage(
            active_offers_complete=True,
            active_credits_complete=True,
            wallets_complete=True,
            offer_history_complete=history_complete,
            offer_history_pages=1 if history_complete else 0,
            offer_history_start_ms=finished_at - 1000 if history_complete else None,
            offer_history_end_ms=finished_at if history_complete else None,
        ),
    )


async def _append_snapshot(factory, *, finished_at: int, offers=()) -> int:
    async with factory() as session:
        store = PostgresEventStore(deployment_environment="ci")
        await store.append_snapshot(
            session,
            _snapshot(
                ACCOUNT_ID,
                finished_at=finished_at,
                offers=offers,
            ),
        )
        await session.commit()
    async with factory() as session:
        return await session.scalar(
            select(EventLogRow.event_seq)
            .where(
                EventLogRow.exchange_account_id == ACCOUNT_ID,
                EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED",
            )
            .order_by(EventLogRow.event_seq.desc())
            .limit(1)
        ) or 0


@pytest_asyncio.fixture
async def uncertainty_app(sqlite_engine, monkeypatch):
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as seed:
        await _seed_account(seed, ACCOUNT_ID, "operator-1")
        await _seed_account(seed, OTHER_ACCOUNT_ID, "other-operator")
        seed.add(_decision())
        await seed.flush()
        store = PostgresEventStore(deployment_environment="ci")
        await store.append(seed, _snapshot(ACCOUNT_ID, finished_at=1000))
        reference = ReservationRef(
            execution_decision_id="decision-7",
            cid=7,
            signal_correlation_id=SCID,
        )
        await store.append(
            seed,
            ReservationIntent(
                symbol="fUST",
                cid=7,
                signal_correlation_id=SCID,
                account_id=str(ACCOUNT_ID),
                is_simulated=True,
                execution_decision_id="decision-7",
                reservation_ref=reference,
                submission_attempt=_attempt(),
                amount=Decimal("100"),
                occurred_at_ms=1050,
            ),
        )
        await store.append(
            seed,
            ReservationUnknown(
                symbol="fUST",
                cid=7,
                size_usdt=Decimal("100"),
                signal_correlation_id=SCID,
                account_id=str(ACCOUNT_ID),
                is_simulated=True,
                reason="connection_reset",
                occurred_at_ms=1100,
                reservation_ref=reference,
            ),
        )
        await seed.commit()
    async with factory() as read:
        uncertainty_id = await read.scalar(
            select(ExecutionUncertaintyRow.uncertainty_id).where(
                ExecutionUncertaintyRow.exchange_account_id == ACCOUNT_ID,
                ExecutionUncertaintyRow.symbol == "fUST",
                ExecutionUncertaintyRow.state == "open",
            )
        )

    app = FastAPI()
    from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router

    app.include_router(build_uncertainties_router())

    async def _operator() -> Principal:
        return Principal(user_id="operator-1", email="operator@example.com", role="admin")

    async def _session():
        async with factory() as value:
            try:
                yield value
                await value.commit()
            except Exception:
                await value.rollback()
                raise

    app.dependency_overrides[require_operator] = _operator
    app.dependency_overrides[get_session] = _session
    client = TestClient(app)
    client.uncertainty_id = uncertainty_id  # type: ignore[attr-defined]
    return client, factory


def test_cross_account_uncertainty_is_non_enumerating_404(uncertainty_app) -> None:
    client, _factory = uncertainty_app
    response = client.get(f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainties")
    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"


def test_stale_reconcile_fence_is_rejected(uncertainty_app) -> None:
    client, _factory = uncertainty_app
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={"reconcileEventSeq": 1, "evidence": {"candidateCount": 0}},
    )
    assert response.status_code == 409


def test_mark_not_accepted_appends_resolution_event(uncertainty_app) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "operatorUuid": "operator-1",
            "reason": "complete history had zero exact candidates",
            "evidence": {"candidateCount": 0},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["state"] == "resolved"


def test_bind_to_venue_appends_resolution_event(uncertainty_app) -> None:
    client, factory = uncertainty_app
    offer = VenueOfferObservation(
        venue_offer_id="venue-7",
        symbol="fUST",
        amount_original=Decimal("100"),
        amount_remaining=Decimal("100"),
        rate=Decimal("0.001"),
        period_days=2,
        status="active",
        mts_created=2_000,
        mts_updated=2_000,
    )
    reconcile_seq = asyncio.run(
        _append_snapshot(factory, finished_at=2_000, offers=(offer,))
    )
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/bind-to-venue",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "venueOfferId": "venue-7",
            "operatorUuid": "operator-1",
            "reason": "exact venue offer confirmed",
            "evidence": {"candidateCount": 1},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["state"] == "resolved"


def test_manual_resolution_appends_resolution_event(uncertainty_app) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/manual-resolution",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "operatorUuid": "operator-1",
            "reason": "operator confirmed the venue request was not accepted",
            "evidence": {"decision": "not_accepted"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["state"] == "resolved"


def test_manual_resolution_requires_reason_and_operator_uuid(uncertainty_app) -> None:
    client, _factory = uncertainty_app
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{uuid4()}/manual-resolution",
        json={"reconcileEventSeq": 2, "operatorUuid": "operator-1"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_resolution_event_replay_contract(sqlite_engine) -> None:
    """The event-store registry must eventually round-trip resolution events."""
    from bfx_funding_bot.modules.execution.event_store.serialization import (
        deserialize_event,
        event_type_of,
        serialize_event,
    )
    from bfx_funding_bot.modules.execution.events import UncertaintyMarkedNotAccepted

    event = UncertaintyMarkedNotAccepted(
        uncertainty_id=uuid4(),
        account_id=str(ACCOUNT_ID),
        environment="ci",
        symbol="fUST",
        kind="submit_outcome_unknown",
        reconcile_event_seq=3,
        resolved_by_operator_id="operator-1",
        resolution_reason="venue history had zero exact candidates",
        resolution_evidence={"candidate_count": 0},
    )
    payload = serialize_event(event)
    decoded = deserialize_event(event_type_of(event), payload)
    assert decoded == event
