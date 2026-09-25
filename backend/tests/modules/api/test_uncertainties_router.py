"""Account-scoped uncertainty API contract tests.

These tests intentionally exercise the public router through FastAPI and a real
SQLite projection.  Venue resolution is represented by the event writer; the
service exposes no direct projection-resolution mutation path.
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
    VenueOfferQuarantined,
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
            "flags": 0,
        },
        started_at_ms=1000,
    )


def _snapshot(
    account_id: UUID,
    *,
    finished_at: int,
    started_at: int | None = None,
    offers: tuple[VenueOfferObservation, ...] = (),
    offer_history: tuple[VenueOfferObservation, ...] = (),
    history_complete: bool = True,
) -> VenueSnapshotObserved:
    history_mts = [offer.mts_created for offer in offer_history]
    return VenueSnapshotObserved(
        account_id=str(account_id),
        environment="ci",
        query_started_at_ms=finished_at - 10 if started_at is None else started_at,
        query_finished_at_ms=finished_at,
        offers=offers,
        offer_history=offer_history,
        credits=(),
        wallet_available={"fUST": Decimal("500")},
        coverage=SnapshotCoverage(
            active_offers_complete=True,
            active_credits_complete=True,
            wallets_complete=True,
            offer_history_complete=history_complete,
            offer_history_pages=1 if history_complete else 0,
            offer_history_start_ms=finished_at - 1000 if history_complete else None,
            offer_history_end_ms=(finished_at - 10 if started_at is None else started_at)
            if history_complete
            else None,
            offer_history_oldest_mts=min(history_mts) if history_mts else None,
            offer_history_newest_mts=max(history_mts) if history_mts else None,
        ),
    )


async def _append_snapshot(
    factory,
    *,
    finished_at: int,
    started_at: int | None = None,
    offers=(),
    offer_history=(),
) -> int:
    async with factory() as session:
        store = PostgresEventStore(deployment_environment="ci")
        await store.append_snapshot(
            session,
            _snapshot(
                ACCOUNT_ID,
                finished_at=finished_at,
                started_at=started_at,
                offers=offers,
                offer_history=offer_history,
            ),
        )
        await session.commit()
    async with factory() as session:
        return (
            await session.scalar(
                select(EventLogRow.event_seq)
                .where(
                    EventLogRow.exchange_account_id == ACCOUNT_ID,
                    EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED",
                )
                .order_by(EventLogRow.event_seq.desc())
                .limit(1)
            )
            or 0
        )


async def _latest_resolution_payload(factory) -> dict[str, object]:
    async with factory() as session:
        row = await session.scalar(
            select(EventLogRow)
            .where(EventLogRow.event_type == "UNCERTAINTY_MARKED_NOT_ACCEPTED")
            .order_by(EventLogRow.event_seq.desc())
            .limit(1)
        )
        assert row is not None
        return row.payload


async def _seed_orphan_uncertainty(factory, offer: VenueOfferObservation) -> UUID:
    async with factory() as session:
        store = PostgresEventStore(deployment_environment="ci")
        await store.append_snapshot(
            session,
            _snapshot(ACCOUNT_ID, finished_at=2_000, offers=(offer,)),
        )
        await store.append(
            session,
            VenueOfferQuarantined(
                venue_offer_id=offer.venue_offer_id,
                symbol=offer.symbol,
                amount=offer.amount_remaining,
                account_id=str(ACCOUNT_ID),
                observed_at_ms=2_001,
            ),
        )
        await session.commit()
    async with factory() as session:
        value = await session.scalar(
            select(ExecutionUncertaintyRow.uncertainty_id).where(
                ExecutionUncertaintyRow.kind == "unattributed_venue_offer",
                ExecutionUncertaintyRow.state == "open",
            )
        )
        assert value is not None
        return value


class _Authority:
    """Records who the worker asked about; ``authorized`` is the verdict."""

    def __init__(self, authorized: set[str] | None = None) -> None:
        self.authorized = {"operator-1"} if authorized is None else authorized
        self.calls: list[tuple[UUID, str]] = []

    async def __call__(self, session, *, account_id: UUID, user: str) -> bool:
        self.calls.append((account_id, user))
        return user in self.authorized


def _apply_queued(factory, *, clock: int = 5_000, authority: _Authority | None = None) -> bool:
    """Run the daemon-side resolution worker once, as the account writer would."""
    from bfx_funding_bot.modules.execution.uncertainty_resolution import (
        ResolutionScope,
        UncertaintyResolutionWorker,
    )

    worker = UncertaintyResolutionWorker(
        session_factory=factory,
        scope=ResolutionScope(ACCOUNT_ID, "ci"),
        authority=authority or _Authority(),
        clock=lambda: clock,
    )
    return asyncio.run(worker.tick())


async def _event_types(factory) -> list[str]:
    async with factory() as session:
        return list(
            await session.scalars(select(EventLogRow.event_type).order_by(EventLogRow.event_seq))
        )


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


def test_uncertainty_read_exposes_exact_server_derived_resolution_context(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    matching = VenueOfferObservation(
        venue_offer_id="venue-read-context",
        symbol="fUST",
        amount_original=Decimal("100"),
        amount_remaining=Decimal("100"),
        rate=Decimal("0.001"),
        period_days=2,
        status="active",
        mts_created=1_500,
        mts_updated=1_500,
        offer_type="LIMIT",
        flags={"raw": 0},
    )
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000, offers=(matching,)))

    response = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}"  # type: ignore[attr-defined]
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["resolutionContext"] == {
        "reconcileEventSeq": reconcile_seq,
        "queryStartedAtMs": 1_990,
        "queryFinishedAtMs": 2_000,
        "candidateCount": 1,
        "candidateVenueOfferIds": ["venue-read-context"],
        "unavailableReason": None,
    }


def test_uncertainty_list_exposes_zero_match_context_for_explicit_confirmation(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))

    response = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties",
        params={"state": "open"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["resolutionContext"] == {
        "reconcileEventSeq": reconcile_seq,
        "queryStartedAtMs": 1_990,
        "queryFinishedAtMs": 2_000,
        "candidateCount": 0,
        "candidateVenueOfferIds": [],
        "unavailableReason": None,
    }


def test_uncertainty_read_exposes_all_server_derived_ambiguous_candidates(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app

    def matching(venue_offer_id: str) -> VenueOfferObservation:
        return VenueOfferObservation(
            venue_offer_id=venue_offer_id,
            symbol="fUST",
            amount_original=Decimal("100"),
            amount_remaining=Decimal("100"),
            rate=Decimal("0.001"),
            period_days=2,
            status="active",
            mts_created=1_500,
            mts_updated=1_500,
            offer_type="LIMIT",
            flags={"raw": 0},
        )

    reconcile_seq = asyncio.run(
        _append_snapshot(
            factory,
            finished_at=2_000,
            offers=(matching("venue-b"), matching("venue-a")),
        )
    )

    response = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}"  # type: ignore[attr-defined]
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["resolutionContext"] == {
        "reconcileEventSeq": reconcile_seq,
        "queryStartedAtMs": 1_990,
        "queryFinishedAtMs": 2_000,
        "candidateCount": 2,
        "candidateVenueOfferIds": ["venue-a", "venue-b"],
        "unavailableReason": "multiple_exact_candidates",
    }


def test_uncertainty_read_bounds_candidate_ids_but_preserves_full_count(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app

    def matching(index: int) -> VenueOfferObservation:
        return VenueOfferObservation(
            venue_offer_id=f"venue-{index:02}",
            symbol="fUST",
            amount_original=Decimal("100"),
            amount_remaining=Decimal("100"),
            rate=Decimal("0.001"),
            period_days=2,
            status="active",
            mts_created=1_500,
            mts_updated=1_500,
            offer_type="LIMIT",
            flags={"raw": 0},
        )

    asyncio.run(
        _append_snapshot(
            factory,
            finished_at=2_000,
            offers=tuple(matching(index) for index in range(20)),
        )
    )

    response = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}"  # type: ignore[attr-defined]
    )

    assert response.status_code == 200, response.text
    context = response.json()["data"]["resolutionContext"]
    assert context["candidateCount"] == 20
    assert context["candidateVenueOfferIds"] == [
        f"venue-{index:02}" for index in range(16)
    ]
    assert context["unavailableReason"] == "multiple_exact_candidates"


def test_uncertainty_read_fails_closed_when_latest_snapshot_is_not_authoritative(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    asyncio.run(_append_snapshot(factory, finished_at=3_000))
    delayed_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))

    response = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}"  # type: ignore[attr-defined]
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["resolutionContext"] == {
        "reconcileEventSeq": delayed_seq,
        "queryStartedAtMs": 1_990,
        "queryFinishedAtMs": 2_000,
        "candidateCount": None,
        "candidateVenueOfferIds": [],
        "unavailableReason": "stale_reconcile_fence",
    }


def test_mark_not_accepted_appends_resolution_event(uncertainty_app) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "operatorUuid": "operator-1",
            "reason": "complete history had zero exact candidates",
            "evidence": {"candidateCount": 999},
        },
    )
    assert response.status_code == 202, response.text
    queued = response.json()["data"]
    assert queued["state"] == "requested"
    assert queued["action"] == "mark_not_accepted"
    assert "UNCERTAINTY_MARKED_NOT_ACCEPTED" not in asyncio.run(_event_types(factory))

    assert _apply_queued(factory) is True
    outcome = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainty-resolution-requests/{queued['requestId']}"
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["data"]["state"] == "applied"
    assert outcome.json()["data"]["outcomeReason"] is None
    resolved = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}"  # type: ignore[attr-defined]
    )
    assert resolved.json()["data"]["state"] == "resolved"
    assert resolved.json()["data"]["resolvedEventSeq"] == outcome.json()["data"]["resolvedEventSeq"]
    stored = asyncio.run(_latest_resolution_payload(factory))
    assert stored["resolution_evidence"] == {
        "reconcile_event_seq": reconcile_seq,
        "query_started_at_ms": 1_990,
        "query_finished_at_ms": 2_000,
        "candidate_count": 0,
    }


def test_mark_not_accepted_recomputes_candidates_instead_of_trusting_request(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    matching = VenueOfferObservation(
        venue_offer_id="venue-hidden-by-forged-count",
        symbol="fUST",
        amount_original=Decimal("100"),
        amount_remaining=Decimal("0"),
        rate=Decimal("0.001"),
        period_days=2,
        status="executed",
        mts_created=1_500,
        mts_updated=1_500,
        offer_type="LIMIT",
        flags={"raw": 0},
    )
    reconcile_seq = asyncio.run(
        _append_snapshot(
            factory,
            finished_at=2_000,
            offer_history=(matching,),
        )
    )

    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "operatorUuid": "operator-1",
            "reason": "forged zero",
            "evidence": {"candidateCount": 0},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "venue_offer_match_not_zero"


def test_mark_not_accepted_fails_closed_for_legacy_offer_missing_type(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    legacy = VenueOfferObservation(
        venue_offer_id="venue-legacy",
        symbol="fUST",
        amount_original=Decimal("100"),
        amount_remaining=Decimal("0"),
        rate=Decimal("0.001"),
        period_days=2,
        status="executed",
        mts_created=1_500,
        mts_updated=1_500,
        offer_type=None,
        flags={"raw": 0},
    )
    reconcile_seq = asyncio.run(
        _append_snapshot(factory, finished_at=2_000, offer_history=(legacy,))
    )

    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={"reconcileEventSeq": reconcile_seq, "evidence": {"candidateCount": 0}},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "venue_offer_match_not_zero"


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
        mts_created=1_500,
        mts_updated=1_500,
        offer_type="LIMIT",
        flags={"raw": 0},
    )
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000, offers=(offer,)))
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
    assert response.status_code == 202, response.text
    assert response.json()["data"]["state"] == "requested"
    assert _apply_queued(factory) is True
    request_id = response.json()["data"]["requestId"]
    outcome = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainty-resolution-requests/{request_id}"
    )
    assert outcome.json()["data"]["state"] == "applied"
    assert asyncio.run(_event_types(factory))[-1] == "UNCERTAINTY_BOUND_TO_VENUE_OFFER"


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("amount_original", Decimal("99")),
        ("rate", Decimal("0.002")),
        ("period_days", 30),
        ("offer_type", "FRRDELTA"),
        ("flags", {"raw": 1}),
        ("mts_created", 999),
    ],
)
def test_bind_requires_full_immutable_attempt_identity(
    uncertainty_app,
    changed_field: str,
    changed_value: object,
) -> None:
    client, factory = uncertainty_app
    values = {
        "venue_offer_id": "venue-same-id",
        "symbol": "fUST",
        "amount_original": Decimal("100"),
        "amount_remaining": Decimal("100"),
        "rate": Decimal("0.001"),
        "period_days": 2,
        "status": "active",
        "mts_created": 1_500,
        "mts_updated": 1_500,
        "offer_type": "LIMIT",
        "flags": {"raw": 0},
    }
    values[changed_field] = changed_value
    if changed_field == "amount_original":
        values["amount_remaining"] = changed_value
    offer = VenueOfferObservation(**values)  # type: ignore[arg-type]
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000, offers=(offer,)))

    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/bind-to-venue",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "venueOfferId": "venue-same-id",
            "operatorUuid": "operator-1",
            "evidence": {"candidateCount": 1},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "venue_offer_match_not_exact"


def test_bind_rejects_multiple_exact_candidates_even_when_requested_id_exists(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app

    def offer(venue_offer_id: str) -> VenueOfferObservation:
        return VenueOfferObservation(
            venue_offer_id=venue_offer_id,
            symbol="fUST",
            amount_original=Decimal("100"),
            amount_remaining=Decimal("100"),
            rate=Decimal("0.001"),
            period_days=2,
            status="active",
            mts_created=1_500,
            mts_updated=1_500,
            offer_type="LIMIT",
            flags={"raw": 0},
        )

    reconcile_seq = asyncio.run(
        _append_snapshot(
            factory,
            finished_at=2_000,
            offers=(offer("venue-requested"), offer("venue-other")),
        )
    )
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/bind-to-venue",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "venueOfferId": "venue-requested",
            "operatorUuid": "operator-1",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "venue_offer_match_not_exact"


def test_submit_unknown_cannot_use_manual_resolution_escape_hatch(
    uncertainty_app,
) -> None:
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
    assert response.status_code == 409
    assert response.json()["detail"] == "resolution_action_not_supported"


def test_manual_resolution_rejects_non_allowlisted_decision(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    orphan = VenueOfferObservation(
        venue_offer_id="invalid-decision-offer",
        symbol="fUST",
        amount_original=Decimal("25"),
        amount_remaining=Decimal("25"),
        rate=Decimal("0.001"),
        period_days=2,
        status="active",
        mts_created=1_500,
        mts_updated=1_500,
        offer_type="LIMIT",
        flags={"raw": 0},
    )
    uncertainty_id = asyncio.run(_seed_orphan_uncertainty(factory, orphan))
    reconcile_seq = asyncio.run(
        _append_snapshot(factory, finished_at=3_000, offers=(orphan,))
    )

    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{uncertainty_id}/manual-resolution",
        json={
            "reconcileEventSeq": reconcile_seq,
            "operatorUuid": "operator-1",
            "reason": "invalid client decision",
            "evidence": {"decision": "accept"},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_manual_resolution_decision"


def test_manual_resolution_allows_unattributed_venue_offer(uncertainty_app) -> None:
    client, factory = uncertainty_app
    orphan = VenueOfferObservation(
        venue_offer_id="manual-venue-offer",
        symbol="fUST",
        amount_original=Decimal("25"),
        amount_remaining=Decimal("25"),
        rate=Decimal("0.001"),
        period_days=2,
        status="active",
        mts_created=1_500,
        mts_updated=1_500,
        offer_type="LIMIT",
        flags={"raw": 0},
    )

    orphan_uncertainty_id = asyncio.run(_seed_orphan_uncertainty(factory, orphan))
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=3_000, offers=(orphan,)))
    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{orphan_uncertainty_id}/manual-resolution",
        json={
            "reconcileEventSeq": reconcile_seq,
            "operatorUuid": "operator-1",
            "reason": "operator accepted the manual venue offer",
            "evidence": {"decision": "accepted_external_exposure"},
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["data"]["state"] == "requested"
    assert _apply_queued(factory) is True

    async def load_resolution_action() -> object:
        async with factory() as session:
            event = await session.scalar(
                select(EventLogRow)
                .where(EventLogRow.event_type == "UNCERTAINTY_MANUALLY_RESOLVED")
                .order_by(EventLogRow.event_seq.desc())
                .limit(1)
            )
            assert event is not None
            return event.payload["resolution_action"]

    assert asyncio.run(load_resolution_action()) == "accepted_external_exposure"


def test_resolution_rejects_snapshot_whose_query_overlaps_opening_event(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, started_at=1_100, finished_at=1_200))

    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={"reconcileEventSeq": reconcile_seq, "evidence": {"candidateCount": 0}},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "stale_reconcile_fence"


def test_resolution_rejects_latest_appended_snapshot_older_than_projected_authority(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    asyncio.run(_append_snapshot(factory, finished_at=3_000))
    delayed_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))

    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={"reconcileEventSeq": delayed_seq, "evidence": {"candidateCount": 0}},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "stale_reconcile_fence"


def test_resolution_evidence_rejects_unknown_secret_like_fields(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))

    response = client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",  # type: ignore[attr-defined]
        json={
            "reconcileEventSeq": reconcile_seq,
            "evidence": {"candidateCount": 0, "apiSecret": "must-not-persist"},
        },
    )

    assert response.status_code == 422


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


def _mark_not_accepted(client, reconcile_seq: int, *, reason: str = "zero candidates"):
    return client.post(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}/mark-not-accepted",
        json={
            "reconcileEventSeq": reconcile_seq,
            "operatorUuid": "operator-1",
            "reason": reason,
            "evidence": {"candidateCount": 0},
        },
    )


def _request_outcome(client, request_id: str) -> dict[str, object]:
    response = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainty-resolution-requests/{request_id}"
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_queued_request_is_visible_as_pending_until_the_worker_applies_it(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    queued = _mark_not_accepted(client, reconcile_seq)
    assert queued.status_code == 202, queued.text
    request_id = queued.json()["data"]["requestId"]

    listed = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties", params={"state": "open"}
    ).json()["data"]
    assert [row["resolutionRequest"]["requestId"] for row in listed] == [request_id]
    assert listed[0]["resolutionRequest"]["state"] == "requested"
    assert listed[0]["state"] == "open"

    assert _apply_queued(factory) is True
    assert _apply_queued(factory) is False  # nothing left: one request, one event
    events = asyncio.run(_event_types(factory))
    assert events.count("UNCERTAINTY_MARKED_NOT_ACCEPTED") == 1
    assert client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties", params={"state": "open"}
    ).json()["data"] == []


def test_identical_repeat_is_the_same_request_and_a_different_one_waits(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    first = _mark_not_accepted(client, reconcile_seq)
    repeat = _mark_not_accepted(client, reconcile_seq)
    assert first.status_code == repeat.status_code == 202
    assert repeat.json()["data"]["requestId"] == first.json()["data"]["requestId"]

    different = _mark_not_accepted(client, reconcile_seq, reason="another reason")
    assert different.status_code == 409
    assert different.json()["detail"] == "resolution_request_pending"

    assert _apply_queued(factory) is True
    after = _mark_not_accepted(client, reconcile_seq)
    assert after.status_code == 409
    assert after.json()["detail"] == "uncertainty_already_resolved"
    assert asyncio.run(_event_types(factory)).count("UNCERTAINTY_MARKED_NOT_ACCEPTED") == 1


def test_worker_records_why_an_accepted_request_no_longer_applies(
    uncertainty_app,
) -> None:
    """Evidence can move between acceptance and apply; say so, never append."""
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    queued = _mark_not_accepted(client, reconcile_seq)
    assert queued.status_code == 202
    asyncio.run(_append_snapshot(factory, finished_at=3_000))  # a newer fence arrives

    assert _apply_queued(factory) is True
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "rejected"
    assert outcome["outcomeReason"] == "stale_reconcile_fence"
    assert outcome["resolvedEventSeq"] is None
    assert "UNCERTAINTY_MARKED_NOT_ACCEPTED" not in asyncio.run(_event_types(factory))
    detail = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}"  # type: ignore[attr-defined]
    ).json()["data"]
    assert detail["state"] == "open"
    # The console learns why from the list itself, not a separate poll.
    assert detail["resolutionRequest"]["requestId"] == queued.json()["data"]["requestId"]
    assert detail["resolutionRequest"]["state"] == "rejected"
    assert detail["resolutionRequest"]["outcomeReason"] == "stale_reconcile_fence"


def test_worker_failure_is_recorded_with_its_root_cause_not_a_bare_conflict(
    uncertainty_app, monkeypatch
) -> None:
    """The 2026-09-21 permission failure surfaced only as `resolution_rejected`."""
    from bfx_funding_bot.modules.execution.event_store.writer import (
        AccountEventWriter,
        ProjectionWriteError,
    )

    class InsufficientPrivilegeError(Exception):
        pass

    async def denied(self, session, event):
        try:
            raise InsufficientPrivilegeError("permission denied for table projection_heads")
        except InsufficientPrivilegeError as exc:
            raise ProjectionWriteError("serialized projection failed") from exc

    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    queued = _mark_not_accepted(client, reconcile_seq)
    original = AccountEventWriter.append
    monkeypatch.setattr(AccountEventWriter, "append", denied)

    assert _apply_queued(factory) is True
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "failed"
    assert outcome["outcomeReason"] == "projection_write_failed:InsufficientPrivilegeError"
    assert "UNCERTAINTY_MARKED_NOT_ACCEPTED" not in asyncio.run(_event_types(factory))

    # A failed request is terminal; the operator may ask again.
    monkeypatch.setattr(AccountEventWriter, "append", original)
    retry = _mark_not_accepted(client, reconcile_seq)
    assert retry.status_code == 202, retry.text
    assert retry.json()["data"]["requestId"] != queued.json()["data"]["requestId"]
    assert _apply_queued(factory) is True
    assert _request_outcome(client, retry.json()["data"]["requestId"])["state"] == "applied"


def test_only_the_configured_operator_can_queue_an_adjudication() -> None:
    """The web API admits exactly who the worker will authorize.

    `require_operator` (configured operator id + admin role) guards every
    adjudication route through `require_account_member`, so a writer who is not
    the operator is refused with 403 before anything is queued -- never accepted
    and later rejected as `operator_not_authorized`. The worker's database check then
    only catches what changed since: ban, lost TOTP, lost membership.
    """
    from fastapi.dependencies.models import Dependant
    from fastapi.routing import APIRoute

    from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router

    def calls(dependant: Dependant) -> set[object]:
        found = {dependant.call}
        for child in dependant.dependencies:
            found |= calls(child)
        return found

    posts = [
        route for route in build_uncertainties_router().routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    ]
    assert {route.path.rsplit("/", 1)[-1] for route in posts} == {
        "bind-to-venue", "mark-not-accepted", "manual-resolution",
    }
    for route in posts:
        assert require_operator in calls(route.dependant), route.path


def test_resolution_request_reads_are_account_scoped(uncertainty_app) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    request_id = _mark_not_accepted(client, reconcile_seq).json()["data"]["requestId"]
    denied = client.get(
        f"/api/v1/exchange-accounts/{OTHER_ACCOUNT_ID}/uncertainty-resolution-requests/{request_id}"
    )
    assert denied.status_code == 404
    missing = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainty-resolution-requests/{uuid4()}"
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "not_found"


async def _queue_directly(factory, *, uncertainty_id: UUID, created_at_ms: int) -> UUID:
    """A request the web API would have refused; the worker must still settle it."""
    from sqlalchemy import insert

    from bfx_funding_bot.modules.execution.uncertainty_tables import (
        UncertaintyResolutionRequestRow,
    )

    request_id = uuid4()
    async with factory.begin() as session:
        await session.execute(insert(UncertaintyResolutionRequestRow).values(
            request_id=request_id, exchange_account_id=ACCOUNT_ID, deployment_environment="ci",
            uncertainty_id=uncertainty_id, action="mark_not_accepted", reconcile_event_seq=1,
            requested_by="operator-1", created_at_ms=created_at_ms,
        ))
    return request_id


def test_a_request_whose_outcome_cannot_be_written_does_not_block_the_queue(
    uncertainty_app, monkeypatch
) -> None:
    """Head-of-line poison: the outcome write fails, so the tick used to roll back
    and the same oldest row was picked again forever, starving every later one."""
    from bfx_funding_bot.modules.execution.event_store.writer import (
        AccountEventWriter,
        AppendResult,
    )

    async def unrecordable(self, session, event):
        # An append result the outcome row cannot hold: `applied` needs a seq.
        return AppendResult(
            event_seq=None, persisted=True, projection_head=None,  # type: ignore[arg-type]
            projector_version="execution-state-v1",
        )

    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    poison = _mark_not_accepted(client, reconcile_seq).json()["data"]["requestId"]
    later = asyncio.run(_queue_directly(factory, uncertainty_id=uuid4(), created_at_ms=10**15))
    monkeypatch.setattr(AccountEventWriter, "append", unrecordable)

    assert _apply_queued(factory) is True
    first = _request_outcome(client, poison)
    assert first["state"] == "failed"
    assert str(first["outcomeReason"]).startswith("outcome_write_failed:")
    assert first["resolvedEventSeq"] is None

    assert _apply_queued(factory) is True
    assert _request_outcome(client, str(later))["state"] == "rejected"
    assert _apply_queued(factory) is False


def test_worker_skips_a_request_it_cannot_even_mark_failed(uncertainty_app, monkeypatch) -> None:
    """If the fallback write also fails, the worker moves on rather than spin."""
    from bfx_funding_bot.modules.execution.event_store.writer import (
        AccountEventWriter,
        AppendResult,
    )
    from bfx_funding_bot.modules.execution.uncertainty_resolution import (
        ResolutionScope,
        UncertaintyResolutionWorker,
    )

    async def unrecordable(self, session, event):
        return AppendResult(
            event_seq=None, persisted=True, projection_head=None,  # type: ignore[arg-type]
            projector_version="execution-state-v1",
        )

    async def cannot_mark(self, request_id, reason):
        raise RuntimeError("database unavailable")

    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    poison = _mark_not_accepted(client, reconcile_seq).json()["data"]["requestId"]
    later = asyncio.run(_queue_directly(factory, uncertainty_id=uuid4(), created_at_ms=10**15))
    monkeypatch.setattr(AccountEventWriter, "append", unrecordable)
    monkeypatch.setattr(UncertaintyResolutionWorker, "_mark_failed", cannot_mark)
    worker = UncertaintyResolutionWorker(
        session_factory=factory, scope=ResolutionScope(ACCOUNT_ID, "ci"),
        authority=_Authority(), clock=lambda: 5_000,
    )

    async def two_ticks() -> tuple[bool, bool]:
        return await worker.tick(), await worker.tick()

    assert asyncio.run(two_ticks()) == (True, True)
    assert _request_outcome(client, poison)["state"] == "requested"  # left for a restart
    assert _request_outcome(client, str(later))["state"] == "rejected"


def test_worker_only_touches_its_own_account(uncertainty_app) -> None:
    from bfx_funding_bot.modules.execution.uncertainty_resolution import (
        ResolutionScope,
        UncertaintyResolutionWorker,
    )

    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    request_id = _mark_not_accepted(client, reconcile_seq).json()["data"]["requestId"]
    other = UncertaintyResolutionWorker(
        session_factory=factory,
        scope=ResolutionScope(OTHER_ACCOUNT_ID, "ci"),
        authority=_Authority(),
        clock=lambda: 5_000,
    )
    assert asyncio.run(other.tick()) is False
    assert _request_outcome(client, request_id)["state"] == "requested"


def test_revoked_operator_request_is_rejected_without_an_event(uncertainty_app) -> None:
    """Authority is re-read when the daemon applies, not trusted from acceptance."""
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    queued = _mark_not_accepted(client, reconcile_seq)
    assert queued.status_code == 202, queued.text

    revoked = _Authority(authorized=set())
    assert _apply_queued(factory, authority=revoked) is True
    assert revoked.calls == [(ACCOUNT_ID, "operator-1")]
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "rejected"
    assert outcome["outcomeReason"] == "operator_not_authorized"
    assert outcome["resolvedEventSeq"] is None
    assert "UNCERTAINTY_MARKED_NOT_ACCEPTED" not in asyncio.run(_event_types(factory))
    detail = client.get(
        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/{client.uncertainty_id}"  # type: ignore[attr-defined]
    ).json()["data"]
    assert detail["state"] == "open"


def test_authorized_operator_request_is_applied_after_the_authority_check(
    uncertainty_app,
) -> None:
    client, factory = uncertainty_app
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    queued = _mark_not_accepted(client, reconcile_seq)

    authority = _Authority()
    assert _apply_queued(factory, authority=authority) is True
    assert authority.calls == [(ACCOUNT_ID, "operator-1")]
    outcome = _request_outcome(client, queued.json()["data"]["requestId"])
    assert outcome["state"] == "applied"
    assert asyncio.run(_event_types(factory)).count("UNCERTAINTY_MARKED_NOT_ACCEPTED") == 1


@pytest.mark.parametrize(
    ("configured", "role", "user"),
    [
        ("operator-1", "admin", "someone-else"),
        ("operator-1", "viewer", "operator-1"),
        ("", "admin", ""),
    ],
)
def test_shared_operator_authority_refuses_before_asking_the_database(
    monkeypatch, configured: str, role: str, user: str
) -> None:
    """Configured sole admin first; the database function is never reached."""
    from unittest.mock import AsyncMock

    from bfx_funding_bot.modules.execution.operator_requests import operator_authorized

    monkeypatch.delenv("BFX_PHASE", raising=False)
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", configured)
    monkeypatch.setenv("BFX_OPERATOR_ROLE", role)
    session = AsyncMock()
    assert asyncio.run(operator_authorized(session, account_id=ACCOUNT_ID, user=user)) is False
    session.scalar.assert_not_called()


def test_shared_operator_authority_defers_to_the_database_verdict(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from bfx_funding_bot.modules.execution.operator_requests import operator_authorized

    monkeypatch.delenv("BFX_PHASE", raising=False)
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")
    monkeypatch.setenv("BFX_OPERATOR_ROLE", "admin")
    for verdict in (True, False):
        session = AsyncMock()
        session.scalar.return_value = verdict
        assert asyncio.run(
            operator_authorized(session, account_id=ACCOUNT_ID, user="operator-1")
        ) is verdict
        _statement, params = session.scalar.call_args.args
        assert params == {"account": ACCOUNT_ID, "actor": "operator-1"}
