"""Durable submission-attempt and uncertainty projection contracts."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmissionAttemptPayload,
    SubmitOutcomeKind,
)
from bfx_funding_bot.modules.execution.uncertainties import (
    UncertaintyKind,
    UncertaintyService,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    SubmissionAttemptRow,
)

ACCOUNT_ID = UUID("d1a4f3d7-8d6d-4b6a-a5c5-91e1d0eecab1")
ENVIRONMENT = "ci"
SYMBOL = "fUST"


@pytest_asyncio.fixture
async def session_factory(sqlite_engine) -> async_sessionmaker[AsyncSession]:
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(ExchangeAccount(id=ACCOUNT_ID, venue="bitfinex", label="ci"))
        session.add(
            PositionStateRow(
                account_id=str(ACCOUNT_ID),
                exchange_account_id=ACCOUNT_ID,
                deployment_environment=ENVIRONMENT,
                symbol=SYMBOL,
                offered_amount=Decimal("0"),
                lent_amount=Decimal("0"),
                available_amount=Decimal("0"),
                uncertain_amount=Decimal("0"),
                reserved=Decimal("0"),
                realized=Decimal("0"),
                last_updated_ms=0,
                last_event_seq=0,
            )
        )
        session.add_all(
            [_event(seq=10, event_type="SUBMIT_OUTCOME_UNKNOWN"), _event(seq=11)]
        )
        await session.commit()
    return factory


def _event(seq: int, event_type: str = "VENUE_SNAPSHOT_OBSERVED") -> EventLogRow:
    return EventLogRow(
        event_seq=seq,
        account_id=str(ACCOUNT_ID),
        exchange_account_id=ACCOUNT_ID,
        deployment_environment=ENVIRONMENT,
        event_type=event_type,
        payload={},
        occurred_at_ms=seq,
    )


def _attempt_payload(*, decision_id: str = "decision-1") -> SubmissionAttemptPayload:
    return SubmissionAttemptPayload(
        execution_decision_id=decision_id,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        symbol=SYMBOL,
        cid=42,
        normalized_payload={"amount": "12.50", "type": "LIMIT"},
        started_at_ms=100,
        completed_at_ms=101,
        outcome_kind=SubmitOutcomeKind.UNKNOWN,
        outcome_reason="transport_timeout",
        last_event_seq=10,
    )


@pytest.mark.asyncio
async def test_record_attempt_persists_immutable_payload_digest_once_per_decision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A retry must resume the original audit identity, not write a second attempt."""
    service = UncertaintyService(session_factory)
    payload = _attempt_payload()

    first = await service.record_attempt(payload)
    same = await service.record_attempt(payload)

    assert same.attempt_id == first.attempt_id
    assert first.payload_sha256 == payload.payload_fingerprint
    assert dict(first.normalized_payload) == {"amount": "12.50", "type": "LIMIT"}

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SubmissionAttemptRow).where(
                    SubmissionAttemptRow.execution_decision_id == "decision-1"
                )
            )
        ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_open_or_get_is_idempotent_and_reserves_amount_only_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replayed UNKNOWN evidence must not double-count pessimistic exposure."""
    service = UncertaintyService(session_factory)
    attempt = await service.record_attempt(_attempt_payload())
    args = {
        "exchange_account_id": ACCOUNT_ID,
        "deployment_environment": ENVIRONMENT,
        "symbol": SYMBOL,
        "kind": UncertaintyKind.SUBMIT_OUTCOME_UNKNOWN,
        "correlation_key": "attempt:decision-1",
        "intended_amount": Decimal("12.50"),
        "evidence": {"reason": "transport_timeout"},
        "opened_event_seq": 10,
        "attempt_id": attempt.attempt_id,
    }

    first = await service.open_or_get(**args)
    replay = await service.open_or_get(**args)

    assert replay.uncertainty_id == first.uncertainty_id
    async with session_factory() as session:
        position = await session.get(
            PositionStateRow, (ACCOUNT_ID, ENVIRONMENT, SYMBOL)
        )
    assert position is not None
    assert position.uncertain_amount == Decimal("12.50")


@pytest.mark.asyncio
async def test_open_or_get_rejects_negative_amount_and_unknown_kind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Malformed or future vocabulary cannot silently create a safety record."""
    service = UncertaintyService(session_factory)
    common = {
        "exchange_account_id": ACCOUNT_ID,
        "deployment_environment": ENVIRONMENT,
        "symbol": SYMBOL,
        "correlation_key": "venue:9001",
        "opened_event_seq": 10,
    }

    with pytest.raises(ValueError, match="non-negative"):
        await service.open_or_get(
            **common,
            kind=UncertaintyKind.UNATTRIBUTED_VENUE_OFFER,
            intended_amount=Decimal("-1"),
            evidence={"venue_offer_id": "9001"},
        )
    with pytest.raises(ValueError, match="unsupported uncertainty kind"):
        await service.open_or_get(
            **common,
            kind="future_kind",
            intended_amount=Decimal("1"),
            evidence={"venue_offer_id": "9001"},
        )
    with pytest.raises(ValueError, match="evidence exceeds"):
        await service.open_or_get(
            **common,
            kind=UncertaintyKind.UNSUPPORTED_VENUE_EXPOSURE,
            intended_amount=Decimal("1"),
            evidence={"detail": "x" * 20_000},
        )


@pytest.mark.asyncio
async def test_resolve_requires_exact_scope_fresh_reconcile_and_operator_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stale or cross-scope operator action cannot clear a symbol block."""
    service = UncertaintyService(session_factory)
    opened = await service.open_or_get(
        exchange_account_id=ACCOUNT_ID,
        deployment_environment=ENVIRONMENT,
        symbol=SYMBOL,
        kind=UncertaintyKind.UNATTRIBUTED_VENUE_OFFER,
        correlation_key="venue:9001",
        intended_amount=Decimal("4"),
        evidence={"venue_offer_id": "9001"},
        opened_event_seq=10,
        venue_offer_id="9001",
    )

    with pytest.raises(ValueError, match="fresh reconcile"):
        await service.resolve(
            exchange_account_id=ACCOUNT_ID,
            deployment_environment=ENVIRONMENT,
            symbol=SYMBOL,
            kind=UncertaintyKind.UNATTRIBUTED_VENUE_OFFER,
            reconcile_event_seq=10,
            resolved_by_operator_id="operator-1",
            resolution_reason="accepted_manual_offer",
            evidence={"decision": "manual"},
        )
    with pytest.raises(ValueError, match="operator id"):
        await service.resolve(
            exchange_account_id=ACCOUNT_ID,
            deployment_environment=ENVIRONMENT,
            symbol=SYMBOL,
            kind=UncertaintyKind.UNATTRIBUTED_VENUE_OFFER,
            reconcile_event_seq=11,
            resolved_by_operator_id="",
            resolution_reason="accepted_manual_offer",
            evidence={"decision": "manual"},
        )
    with pytest.raises(ValueError, match="open uncertainty"):
        await service.resolve(
            exchange_account_id=ACCOUNT_ID,
            deployment_environment=ENVIRONMENT,
            symbol="fUSD",
            kind=UncertaintyKind.UNATTRIBUTED_VENUE_OFFER,
            reconcile_event_seq=11,
            resolved_by_operator_id="operator-1",
            resolution_reason="accepted_manual_offer",
            evidence={"decision": "manual"},
        )

    resolved = await service.resolve(
        exchange_account_id=ACCOUNT_ID,
        deployment_environment=ENVIRONMENT,
        symbol=SYMBOL,
        kind=UncertaintyKind.UNATTRIBUTED_VENUE_OFFER,
        reconcile_event_seq=11,
        resolved_by_operator_id="operator-1",
        resolution_reason="accepted_manual_offer",
        evidence={"decision": "manual"},
    )

    assert resolved.uncertainty_id == opened.uncertainty_id
    assert resolved.state == "resolved"
    assert resolved.resolved_by_operator_id == "operator-1"
    async with session_factory() as session:
        position = await session.get(
            PositionStateRow, (ACCOUNT_ID, ENVIRONMENT, SYMBOL)
        )
    assert position is not None
    assert position.uncertain_amount == Decimal("0")


def test_submission_attempt_unique_decision_and_open_scope_indexes_are_declared() -> None:
    """DDL, not callers, is the final backstop against duplicate writes."""
    assert SubmissionAttemptRow.__table__.c.execution_decision_id.unique is True
    index_names = {
        index.name
        for index in Base.metadata.tables["execution_uncertainties"].indexes
    }
    assert "uq_execution_uncertainties_open_scope" in index_names
