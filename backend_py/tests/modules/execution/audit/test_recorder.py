from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.audit.model import (
    AuditContext,
    ExecutionDecision,
)
from bfx_funding_bot.modules.execution.audit.recorder import (
    ExecutionAuditUnavailable,
    ExecutionDecisionRecorder,
)
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.contracts import (
    DecisionOutcome,
    ExecutionPolicy,
)


def _ready_audit_decision() -> ExecutionDecision:
    """A persisted READY audit must retain the evidence needed for reconstruction."""
    context = AuditContext(
        account_id="acct-1",
        deployment_environment="ci",
        reconcile_id="reconcile-7",
        cell_id="cell-3",
        symbol="fUST",
        signal_correlation_id="signal-11",
        service_version="service-abc",
        config_hash="sha256:config",
    )
    return ExecutionDecision(
        decision_id="decision-9",
        account_id=context.account_id,
        deployment_environment=context.deployment_environment,
        reconcile_id=context.reconcile_id,
        cell_id=context.cell_id,
        symbol=context.symbol,
        signal_correlation_id=context.signal_correlation_id,
        outcome=DecisionOutcome.READY,
        reason_code=None,
        failed_dependency=None,
        signal_rate=Decimal("0.00020"),
        applied_rate=Decimal("0.00021"),
        amount_usdt=Decimal("100"),
        duration_days=14,
        snapshot_id="book-42",
        snapshot_hash="sha256:book",
        snapshot_captured_at_ms=1_700_000_000_000,
        snapshot_source="ws",
        snapshot_age_ms=42,
        model_version="fill-v1",
        model_hash="sha256:model",
        model_evidence={"fill_probability": "0.91", "samples": 120},
        safety_result={"allowed": True, "guard_name": "risk"},
        execution_policy=ExecutionPolicy.BOOK_GUARDED,
        service_version=context.service_version,
        config_hash=context.config_hash,
        occurred_at_ms=1_700_000_000_042,
        recorded_at_ms=1_700_000_000_043,
    )


@pytest.fixture
async def db_factory(
    sqlite_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(ExecutionDecisionRow.__table__.create)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


class _SqliteDialect:
    name = "sqlite"


class _FailingBind:
    dialect = _SqliteDialect()


class _CommitFailingSession:
    def __init__(self) -> None:
        self.bind = _FailingBind()
        self.rollback_called = False

    async def __aenter__(self) -> _CommitFailingSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: object) -> None:
        del statement

    async def commit(self) -> None:
        raise RuntimeError("commit unavailable")

    async def rollback(self) -> None:
        self.rollback_called = True


class _CommitFailingFactory:
    def __init__(self) -> None:
        self.session = _CommitFailingSession()

    def __call__(self) -> _CommitFailingSession:
        return self.session


@pytest.fixture
def failing_factory() -> _CommitFailingFactory:
    return _CommitFailingFactory()


async def test_ready_decision_is_durable_and_contains_evidence(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Removing the insert or commit would make this cross-session read fail."""
    recorder = ExecutionDecisionRecorder(db_factory)
    decision = _ready_audit_decision()

    await recorder.record(decision)

    async with db_factory() as session:
        row = await session.get(ExecutionDecisionRow, decision.decision_id)
    assert row is not None
    assert row.outcome == "ready"
    assert row.snapshot_id == "book-42"
    assert row.model_hash == "sha256:model"
    assert row.model_evidence == {"fill_probability": "0.91", "samples": 120}


async def test_duplicate_decision_id_is_idempotent_under_retry(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Replacing conflict handling with a plain insert would fail on retry."""
    recorder = ExecutionDecisionRecorder(db_factory)
    decision = _ready_audit_decision()

    await recorder.record(decision)
    await recorder.record(decision)

    async with db_factory() as session:
        rows = (await session.execute(ExecutionDecisionRow.__table__.select())).all()
    assert len(rows) == 1


async def test_audit_commit_failure_raises_typed_error(
    failing_factory: _CommitFailingFactory,
) -> None:
    """A failed commit must fail closed after rolling back the audit transaction."""
    recorder = ExecutionDecisionRecorder(failing_factory)  # type: ignore[arg-type]

    with pytest.raises(ExecutionAuditUnavailable):
        await recorder.record(_ready_audit_decision())

    assert failing_factory.session.rollback_called is True
