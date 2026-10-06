"""UNKNOWN-submit regressions of the frozen legacy event store, and the venue's offer-history
transport the ledger observation reads.

How the ledger resolves an UNKNOWN from an observation is tested in
``test_ledger_unknown_resolver_pg``; the legacy recovery that did it is gone (S1-8).
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    BitfinexAuthREST,
)
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    PositionStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import (
    AccountEventWriter,
    ProjectionWriteError,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationIntent,
    ReservationUnknown,
    SnapshotCoverage,
    UncertaintyMarkedNotAccepted,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
)

from .legacy_history import append_legacy

pytestmark = pytest.mark.integration

_ENV = "ci"
_ACCOUNT = UUID("00000000-0000-0000-0000-000000000041")


class _Coverage:
    requested_start_ms: int
    requested_end_ms: int
    oldest_mts_created: int | None
    newest_mts_created: int | None
    pages: int
    complete: bool


class _History:
    offers: tuple[ActiveFundingOffer, ...]
    coverage: _Coverage


def _offer(
    venue_offer_id: str,
    *,
    symbol: str = "fUST",
    amount: str = "12.50",
    rate: float | None = 0.00031,
    period: int = 2,
    mts_created: int = 1_500,
    status: str = "ACTIVE",
    offer_type: str | None = "LIMIT",
    flags: dict[str, object] | int | None = 0,
) -> ActiveFundingOffer:
    return ActiveFundingOffer(
        venue_offer_id=venue_offer_id,
        symbol=symbol,
        amount=Decimal(amount),
        amount_original=Decimal(amount),
        rate=cast(float, rate),
        period_days=period,
        mts_created=mts_created,
        mts_updated=mts_created,
        status=status,
        offer_type=offer_type,
        flags=flags,
        rate_decimal=Decimal(str(rate)) if rate is not None else None,
    )


def _decision(decision_id: str, signal_id: UUID, *, symbol: str) -> ExecutionDecisionRow:
    return ExecutionDecisionRow(
        decision_id=decision_id,
        account_id=str(_ACCOUNT),
        exchange_account_id=_ACCOUNT,
        deployment_environment=_ENV,
        reconcile_id=f"reconcile-{decision_id}",
        cell_id=f"cell-{symbol}",
        symbol=symbol,
        signal_correlation_id=str(signal_id),
        outcome="ready",
        signal_rate=Decimal("0.00031"),
        applied_rate=Decimal("0.00031"),
        amount_usdt=Decimal("12.50"),
        duration_days=2,
        model_evidence={},
        safety_result={},
        execution_policy="book_guarded",
        service_version="test",
        config_hash="config",
        occurred_at_ms=999,
        recorded_at_ms=999,
    )


async def _seed_attempt(pg_session_factory, *, unknown: bool) -> None:
    signal_id = uuid4()
    decision_id = "decision-unknown"
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="unknown"))
        session.add(_decision(decision_id, signal_id, symbol="fUST"))
        await session.commit()
    attempt = SubmissionAttemptPayload(
        execution_decision_id=decision_id,
        account_id=_ACCOUNT,
        environment=_ENV,
        symbol="fUST",
        cid=41,
        normalized_payload={
            "type": "LIMIT",
            "symbol": "fUST",
            "amount": "12.50",
            "rate": "0.00031",
            "period": 2,
            "flags": 0,
        },
        started_at_ms=1_000,
    )
    intent = ReservationIntent(
        symbol="fUST",
        cid=41,
        signal_correlation_id=signal_id,
        account_id=str(_ACCOUNT),
        is_simulated=False,
        execution_decision_id=decision_id,
        amount=Decimal("12.50"),
        occurred_at_ms=1_000,
        submission_attempt=attempt,
    )
    await append_legacy(pg_session_factory, intent)
    if unknown:
        await append_legacy(
            pg_session_factory,
            ReservationUnknown(
                symbol="fUST",
                cid=41,
                signal_correlation_id=signal_id,
                account_id=str(_ACCOUNT),
                is_simulated=False,
                reason="transport_response_dropped",
                amount=Decimal("12.50"),
                occurred_at_ms=1_001,
                reservation_ref=intent.reservation_ref,
            )
        )


def _empty_snapshot(*, started: int, finished: int) -> VenueSnapshotObserved:
    return VenueSnapshotObserved(
        account_id=str(_ACCOUNT),
        environment=_ENV,
        query_started_at_ms=started,
        query_finished_at_ms=finished,
        offers=(),
        credits=(),
        wallet_available={"fUST": Decimal("100")},
        coverage=SnapshotCoverage(
            active_offers_complete=True,
            active_credits_complete=True,
            wallets_complete=True,
            offer_history_complete=True,
            offer_history_pages=1,
            offer_history_start_ms=1_000,
            offer_history_end_ms=started,
        ),
    )


@pytest.mark.asyncio
async def test_pg_resolution_rejects_snapshot_query_overlapping_opening_event(
    pg_session_factory,
):
    await _seed_attempt(pg_session_factory, unknown=True)
    async with pg_session_factory() as session:
        store = PostgresEventStore(deployment_environment=_ENV)
        result = await store.append_snapshot(
            session,
            _empty_snapshot(started=1_001, finished=2_000),
        )
        uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
        assert result.event_seq is not None
        with pytest.raises(ProjectionWriteError, match="interval is stale"):
            await AccountEventWriter(store=store).append(
                session,
                UncertaintyMarkedNotAccepted(
                    uncertainty_id=uncertainty.uncertainty_id,
                    account_id=str(_ACCOUNT),
                    environment=_ENV,
                    symbol="fUST",
                    kind="submit_outcome_unknown",
                    reconcile_event_seq=result.event_seq,
                    resolved_by_operator_id="operator-1",
                    resolution_reason="overlap",
                    resolution_evidence={},
                    candidate_count=0,
                    occurred_at_ms=2_001,
                ),
            )


@pytest.mark.asyncio
async def test_pg_resolution_rejects_newer_then_late_appended_older_snapshot(
    pg_session_factory,
):
    await _seed_attempt(pg_session_factory, unknown=True)
    async with pg_session_factory() as session:
        store = PostgresEventStore(deployment_environment=_ENV)
        await store.append_snapshot(
            session,
            _empty_snapshot(started=2_000, finished=3_000),
        )
        delayed = await store.append_snapshot(
            session,
            _empty_snapshot(started=1_500, finished=2_500),
        )
        uncertainty = (
            await session.execute(select(ExecutionUncertaintyRow))
        ).scalar_one()
        assert delayed.event_seq is not None
        with pytest.raises(ProjectionWriteError, match="did not advance canonical state"):
            await AccountEventWriter(store=store).append(
                session,
                UncertaintyMarkedNotAccepted(
                    uncertainty_id=uncertainty.uncertainty_id,
                    account_id=str(_ACCOUNT),
                    environment=_ENV,
                    symbol="fUST",
                    kind="submit_outcome_unknown",
                    reconcile_event_seq=delayed.event_seq,
                    resolved_by_operator_id="operator-1",
                    resolution_reason="late append",
                    resolution_evidence={},
                    candidate_count=0,
                    occurred_at_ms=3_001,
                ),
            )


@pytest.mark.asyncio
async def test_snapshot_projector_never_treats_unknown_history_status_as_current(
    pg_session_factory,
):
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="history"))
        await session.commit()
    snapshot = VenueSnapshotObserved(
        account_id=str(_ACCOUNT),
        environment=_ENV,
        query_started_at_ms=4_000,
        query_finished_at_ms=5_000,
        offers=(),
        credits=(),
        wallet_available={"fUST": Decimal("100")},
        coverage=SnapshotCoverage(
            active_offers_complete=True,
            active_credits_complete=True,
            wallets_complete=True,
            offer_history_complete=False,
            offer_history_pages=1,
            offer_history_start_ms=1_000,
            offer_history_end_ms=5_000,
        ),
        offer_history=(
            VenueOfferObservation(
                venue_offer_id="history-mystery",
                symbol="fUST",
                amount_original=Decimal("9"),
                amount_remaining=Decimal("9"),
                rate=Decimal("0.00031"),
                period_days=2,
                status="MYSTERY",
                mts_created=1_500,
                mts_updated=1_500,
            ),
        ),
    )
    async with pg_session_factory() as session:
        await PostgresEventStore(deployment_environment=_ENV).append_snapshot(
            session,
            snapshot,
        )
        await session.commit()

    async with pg_session_factory() as session:
        venue_offer = await session.scalar(
            select(VenueOfferStateRow).where(
                VenueOfferStateRow.venue_offer_id == "history-mystery"
            )
        )
        position = await session.scalar(
            select(PositionStateRow).where(PositionStateRow.symbol == "fUST")
        )
    assert venue_offer is None
    assert position is not None
    assert position.offered_amount == Decimal("0")


def _wire_offer(offer_id: int, mts_created: int) -> list[object | None]:
    row: list[object | None] = [None] * 21
    row[0] = offer_id
    row[1] = "fUST"
    row[2] = mts_created
    row[3] = mts_created
    row[4] = -12.5
    row[5] = -12.5
    row[6] = "LIMIT"
    row[9] = 0
    row[10] = "CANCELED"
    row[14] = 0.00031
    row[15] = 2
    return row


@pytest.mark.asyncio
async def test_history_transport_pages_backward_and_records_complete_coverage_fence():
    """Changing the cursor/end fence must make pagination coverage observable."""
    bodies: list[dict[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json=[_wire_offer(3, 3_000), _wire_offer(2, 2_000)])
        return httpx.Response(200, json=[_wire_offer(1, 1_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            auth_gate=AuthRequestGate(iter((1, 2)).__next__),
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
                allocation_cap_usdt=Decimal("1000"),
            ),
            start_ms=1_000,
            end_ms=5_000,
            limit=2,
        )

    assert bodies == [
        {"start": 1_000, "end": 5_000, "limit": 2},
        {"start": 1_000, "end": 1_999, "limit": 2},
    ]
    assert [offer.venue_offer_id for offer in result.offers] == ["1", "2", "3"]
    assert result.coverage.complete is True
    assert result.coverage.oldest_mts_created == 1_000
    assert result.coverage.newest_mts_created == 3_000
    assert result.coverage.pages == 2


@pytest.mark.asyncio
async def test_history_row_stamped_before_the_fence_keeps_a_short_page_complete():
    """2026-09-29: the venue stamps offers to the whole second, so a window
    starting at .175 returns an offer created at .000. Marking that incomplete
    made the fill unclassifiable and halted trading."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_wire_offer(1, 1_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            auth_gate=AuthRequestGate(iter((1,)).__next__),
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
                allocation_cap_usdt=Decimal("1000"),
            ),
            start_ms=1_175,
            end_ms=5_000,
            limit=2,
        )

    assert result.coverage.complete is True
    assert result.coverage.oldest_mts_created == 1_000


@pytest.mark.asyncio
async def test_full_history_page_at_start_fence_remains_incomplete():
    """A full boundary page may hide more rows sharing the oldest timestamp."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_wire_offer(2, 1_000), _wire_offer(1, 1_000)],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            auth_gate=AuthRequestGate(iter((1,)).__next__),
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
                allocation_cap_usdt=Decimal("1000"),
            ),
            start_ms=1_000,
            end_ms=5_000,
            limit=2,
        )

    assert result.coverage.complete is False
    assert result.coverage.oldest_mts_created == 1_000
    assert result.coverage.pages == 1
