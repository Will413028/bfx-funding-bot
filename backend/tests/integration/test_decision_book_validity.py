"""Original pricing evidence must survive every await until transport."""
import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.audit import AuditContext, ExecutionDecisionRecorder
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeKind
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from bfx_funding_bot.modules.marketfeed.funding_book import FundingBookStore
from tests.integration.test_capital_command_boundary import AMOUNT, boundary
from tests.integration.test_capital_repository import capital_db as capital_db
from tests.integration.test_capital_repository import capital_engine as capital_engine
from tests.modules.execution.deployment.test_reconciler import _Readiness, _valid_snapshot


def book_store(captured=-27900):
    store = FundingBookStore(max_age_seconds=30, clock=lambda: captured)
    store.apply_snapshot("fUST", _valid_snapshot().bids, sequence=10)
    store.apply_sequence("fUST", 11)
    store.apply_checksum("fUST", checksum=123, expected=123, sequence=12)
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "bound", "identity", "symbol", "sequence", "checksum", "future"])
async def test_invalid_original_book_cannot_send(capital_db, fault):
    factory, account = capital_db
    gate, venue, ready, ctx, _, _ = await boundary(factory, account)
    snap = ready.market_snapshot
    if fault == "missing":
        snap = None
    else:
        changes = {
            "bound": {"max_age_ms": None}, "identity": {"snapshot_id": "other"},
            "symbol": {"symbol": "fUSD"}, "sequence": {"sequence_valid": False},
            "checksum": {"checksum_valid": False}, "future": {"captured_at_ms": 1200},
        }
        snap = replace(snap, **changes[fault])
    result = await gate.submit(replace(ready, market_snapshot=snap), ctx)
    assert result.outcome_kind is SubmitOutcomeKind.NOT_SENT
    assert venue.received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", ["none", "lock", "transport_guard"])
@pytest.mark.parametrize("expiring", ["book", "fx"])
async def test_expired_decision_is_durable_not_sent(capital_db, delay, expiring):
    factory, account = capital_db
    gate, venue, base, ctx, _capital, _halt = await boundary(factory, account)
    now = 1100
    gate._clock = lambda: now
    candidate = base.decision.model_copy(update={"offer_amount_usdt": Decimal(AMOUNT)})
    snap = book_store(-27900 if expiring == "book" else 1000).snapshot("fUST", now_ms=now)
    price = PeriodPricer(max_down_pct=Decimal("0.15"), tick=Decimal("0.00000001")).price(
        candidate=candidate, snapshot=snap)
    ready = await ExecutionGate(policy=ExecutionPolicy.BOOK_GUARDED,
        audit=ExecutionDecisionRecorder(factory), readiness=_Readiness()).prepare(
            candidate, decision_id=str(uuid4()), reconcile_id="test", snapshot=snap,
            price=price, fill_evidence=None, safety=GuardResult(True, "test"),
            audit_context=AuditContext(account_id=str(account), deployment_environment="ci",
                reconcile_id="test", cell_id="a30", symbol="fUST",
                signal_correlation_id=str(candidate.signal_correlation_id), service_version="test",
                config_hash="test", strategy="mean_reversion"))
    assert isinstance(ready, ReadyToSubmit)
    ready = replace(ready, capital_view=base.capital_view,
                    funding_amount_evidence=base.funding_amount_evidence)
    if expiring == "fx":
        ready = replace(ready, funding_amount_evidence=replace(ready.funding_amount_evidence,
            requested_at_ms=-27900, received_at_ms=-27900))
    if delay == "transport_guard":
        guard = gate._guard
        async def delayed_guard(*args, **kwargs):
            nonlocal now
            await guard(*args, **kwargs)
            if kwargs.get("transport"):
                now = 3100
        gate._guard = delayed_guard
    if delay == "lock":
        lock = gate._account_locks.setdefault((str(account), "ci"), asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(gate.submit(ready, ctx))
        await asyncio.sleep(0)  # let submit wait on the actual account lock
        assert not task.done()
        now = 3100
        lock.release()
        result = await task
    else:
        result = await gate.submit(ready, ctx)
    expected = SubmitOutcomeKind.ACKNOWLEDGED if delay == "none" else SubmitOutcomeKind.NOT_SENT
    assert result.outcome_kind is expected
    assert len(venue.received) == (1 if delay == "none" else 0)
    async with factory() as session:
        audited = await session.get(ExecutionDecisionRow, ready.decision_id)
        assert audited.snapshot_id == ready.market_snapshot_id
        assert audited.safety_result["book_validity"]["max_age_ms"] == 30000
        assert audited.safety_result["book_validity"]["captured_at_ms"] == snap.captured_at_ms
        attempt = await session.scalar(select(SubmissionAttemptRow).where(
            SubmissionAttemptRow.execution_decision_id == ready.decision_id))
        assert attempt is not None
        assert attempt.outcome_kind == ("acknowledged" if delay == "none" else "not_sent")


@pytest.mark.asyncio
async def test_second_cell_uses_current_clock_for_its_book(capital_db):
    from tests.modules.execution.deployment.test_reconciler import _build, _post_quote
    factory, account = capital_db
    _, _, _, _, runtime, _ = await boundary(factory, account)
    now = 1100
    rec, venue, *_ = _build(exposure=Decimal("0"),
        quotes=[_post_quote("fUST_a30"), _post_quote("fUST_p2")],
        capital_runtime=runtime, book_provider=book_store())
    rec._clock = lambda: now
    submit = venue.submit
    async def delayed_submit(*args, **kwargs):
        nonlocal now
        result = await submit(*args, **kwargs)
        now = 3100
        return result
    venue.submit = delayed_submit
    await rec.deploy()
    assert len(venue.ready_submissions) == 1
