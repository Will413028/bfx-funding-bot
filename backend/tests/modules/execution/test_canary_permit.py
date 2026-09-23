"""Durable one-shot boundary for the Halt 2 canary command."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow  # noqa: F401
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow  # noqa: F401
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow
from bfx_funding_bot.modules.execution.uncertainty_tables import CanaryCommandPermitRow


@pytest.mark.asyncio
async def test_one_shot_gate_rejects_a_second_submit_and_reasserts_halt() -> None:
    from bfx_funding_bot.modules.execution.canary_permit import (
        CanaryOneShotGate,
        CanaryPermitBlocked,
        CanaryPermitScope,
    )
    from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

    class _Permit:
        consumed = 0

        async def consume(self) -> None:
            self.consumed += 1

    class _Inner:
        calls = 0

        async def submit(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return "ack"

    class _Halt:
        calls = 0

        async def reassert(self) -> None:
            self.calls += 1

    permit = _Permit()
    inner = _Inner()
    halt = _Halt()
    halt_authorization = object()
    scope = CanaryPermitScope(
        account_id=UUID("11111111-1111-1111-1111-111111111111"),
        environment="prod",
        symbol="fUST",
        cell="fUST_a30",
        strategy="mean_reversion",
        amount_usdt=Decimal("150"),
    )
    gate = CanaryOneShotGate(
        inner,
        scope=scope,
        halt_authorization=halt_authorization,
        consume_permit=permit.consume,
        reassert_halt=halt.reassert,
    )

    ready = SimpleNamespace(
        decision=SimpleNamespace(symbol="fUST", offer_amount_usdt=150),
        decision_id="decision-1",
    )
    context = AccountContext(
        account_id=str(scope.account_id),
        credentials=Credentials("k", "s"),
        allocation_cap_usdt=Decimal("150"),
    )
    assert await gate.submit(ready, context) == "ack"
    with pytest.raises(CanaryPermitBlocked, match="already_used"):
        await gate.submit(ready, context)

    assert permit.consumed == 1
    assert inner.calls == 1
    assert halt.calls == 1


@pytest.mark.asyncio
async def test_one_shot_gate_authorizes_only_the_consumed_permit_through_persisted_halt() -> None:
    """The real safety chain must admit the one permitted command while halted."""
    from bfx_funding_bot.modules.execution.canary_permit import (
        CanaryOneShotGate,
        CanaryPermitScope,
    )
    from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
    from bfx_funding_bot.modules.execution.safety.hard_guards import ManualKillGuard

    scope = CanaryPermitScope(
        account_id=UUID("11111111-1111-1111-1111-111111111111"),
        environment="prod",
        symbol="fUST",
        cell="fUST_a30",
        strategy="mean_reversion",
        amount_usdt=Decimal("150"),
    )

    class _HaltedStore:
        async def current(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(halted=True, reason="halt2", actor="operator", id=1)

    halt_authorization = object()
    guard = ManualKillGuard(
        halt_store=_HaltedStore(),
        canary_halt_authorization=halt_authorization,
    )

    class _Permit:
        async def consume(self) -> None:
            return None

    class _Inner:
        async def submit(self, ready, context, **_kwargs):  # type: ignore[no-untyped-def]
            result = await guard.evaluate(ready.decision, context)
            assert result.allowed, result.reason
            return "ack"

    gate = CanaryOneShotGate(
        _Inner(),
        scope=scope,
        halt_authorization=halt_authorization,
        consume_permit=_Permit().consume,
        reassert_halt=lambda: _async_noop(),
    )
    ready = SimpleNamespace(
        decision=SimpleNamespace(symbol="fUST", offer_amount_usdt=150),
        decision_id="decision-1",
    )
    context = AccountContext(
        account_id=str(scope.account_id),
        credentials=Credentials("k", "s"),
        allocation_cap_usdt=Decimal("150"),
    )

    assert await gate.submit(ready, context) == "ack"


async def _async_noop() -> None:
    return None


@pytest.mark.asyncio
async def test_durable_permit_is_bound_to_halt_and_consumed_once(sqlite_engine) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from bfx_funding_bot.modules.execution.canary_permit import (
        CanaryPermitBlocked,
        CanaryPermitRepository,
        CanaryPermitScope,
    )

    account_id = UUID("11111111-1111-1111-1111-111111111111")
    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(ExchangeAccount(id=account_id, venue="bitfinex", label="canary"))
        session.add(TradingHaltRow(
            account_id=str(account_id),
            exchange_account_id=account_id,
            deployment_environment="prod",
            halted=True,
            reason="halt2",
            actor="operator-1",
            created_at_ms=100,
        ))
        await session.commit()

    scope = CanaryPermitScope(
        account_id=account_id,
        environment="prod",
        symbol="fUST",
        cell="fUST_a30",
        strategy="mean_reversion",
        amount_usdt=Decimal("150"),
    )
    repository = CanaryPermitRepository(factory)
    permit = await repository.issue(scope, operator_id="operator-1", now_ms=101)
    with pytest.raises(CanaryPermitBlocked, match="already_issued"):
        await repository.issue(scope, operator_id="operator-1", now_ms=102)

    consumed = await repository.consume(permit.permit_id, scope, now_ms=103)
    assert consumed.state == "consumed"
    with pytest.raises(CanaryPermitBlocked, match="already_used"):
        await repository.consume(permit.permit_id, scope, now_ms=104)

    async with factory() as session:
        row = await session.scalar(select(CanaryCommandPermitRow))
    assert row is not None
    assert row.state == "consumed"
