"""TradingStateRepository — the durable ACTIVE / HALTED decision.

The database trigger is the last word on illegal transitions (see
tests/integration/test_trading_state_migration.py); these tests pin that the
repository refuses the same transitions itself, so SQLite fixtures and callers
see the error before PostgreSQL does, and that a decision survives a restart.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.safety.tables  # noqa: F401
from bfx_funding_bot.core.db import Base, make_async_engine_from_url
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.safety.tables import TradingStateRow
from bfx_funding_bot.modules.execution.safety.trading_state import (
    IllegalTradingTransition,
    TradingStateRepository,
)


@pytest_asyncio.fixture
async def scope(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="trading-state"))
    return factory, account


def _repo(factory, account: UUID, env: str = "ci") -> TradingStateRepository:
    return TradingStateRepository(factory, account_id=account, deployment_environment=env)


async def _rows(factory) -> int:
    async with factory() as session:
        return len((await session.scalars(select(TradingStateRow))).all())


@pytest.mark.asyncio
async def test_no_decision_reads_as_none_not_as_active(scope) -> None:
    factory, account = scope
    assert await _repo(factory, account).current() is None


@pytest.mark.asyncio
async def test_transition_is_appended_and_read_back(scope) -> None:
    factory, account = scope
    repo = _repo(factory, account)
    result = await repo.transition("HALTED", cause="operator", actor="will",
                                   reason="candle distortion", now_ms=1000)
    assert result.changed and result.previous is None
    current = await repo.current()
    assert current is not None
    assert (current.state, current.cause, current.actor, current.reason, current.created_at_ms) == (
        "HALTED", "operator", "will", "candle distortion", 1000)
    assert not current.allows_new_offers


@pytest.mark.asyncio
async def test_state_survives_a_restart(tmp_path) -> None:
    """A new process -- new engine, new repository -- reads the same decision."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'state.db'}"
    account = uuid4()
    first = make_async_engine_from_url(url)
    async with first.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(first, expire_on_commit=False)
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="restart"))
    await _repo(factory, account).transition("ACTIVE", cause="operator", actor="will",
                                             reason="trading", now_ms=4)
    written = (await _repo(factory, account).transition(
        "HALTED", cause="operator", actor="will", reason="pg upgrade", now_ms=5)).state
    await first.dispose()

    second = make_async_engine_from_url(url)
    try:
        restarted = await _repo(async_sessionmaker(second, expire_on_commit=False), account).current()
    finally:
        await second.dispose()
    assert restarted == written


@pytest.mark.asyncio
async def test_reasserting_a_halt_keeps_the_halt_in_force(scope) -> None:
    factory, account = scope
    repo = _repo(factory, account)
    first = (await repo.transition("HALTED", cause="operator", actor="will", reason="stop")).state
    again = await repo.transition("HALTED", cause="auto", actor="worker", reason="release_blocked")
    assert not again.changed
    assert again.state == first
    assert await _rows(factory) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "message"), [
    ([("HALTED", "auto"), ("ACTIVE", "auto")], "HALTED -> ACTIVE by auto"),
    # No decision recorded reads as HALTED, so it leaves only the way HALTED does.
    ([("ACTIVE", "auto")], "HALTED -> ACTIVE by auto"),
    # REDUCING and material_deploy are retired; kill_switch was retired before.
    ([("ACTIVE", "operator"), ("REDUCING", "operator")], "unknown trading state"),
    ([("HALTED", "material_deploy")], "unknown trading state cause"),
    ([("HALTED", "kill_switch")], "unknown trading state cause"),
    ([("PAUSED", "operator")], "unknown trading state"),
])
async def test_illegal_transitions_are_rejected_in_code(scope, path, message) -> None:
    factory, account = scope
    repo = _repo(factory, account)
    *setup, (state, cause) = path
    for prior_state, prior_cause in setup:
        await repo.transition(prior_state, cause=prior_cause, actor="setup", reason="setup")
    before = await _rows(factory)
    with pytest.raises(IllegalTradingTransition, match=message):
        await repo.transition(state, cause=cause, actor="test", reason="attempt")
    assert await _rows(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "reason"), [("", "why"), ("will", "  ")])
async def test_evidence_is_enforced(scope, actor, reason) -> None:
    factory, account = scope
    with pytest.raises(IllegalTradingTransition):
        await _repo(factory, account).transition("HALTED", cause="operator", actor=actor,
                                                 reason=reason)
    assert await _rows(factory) == 0


@pytest.mark.asyncio
async def test_legal_lifecycle_is_recorded_in_order(scope) -> None:
    factory, account = scope
    repo = _repo(factory, account)
    steps = [
        ("ACTIVE", "operator"),
        ("HALTED", "operator"),
        ("ACTIVE", "operator"),
        ("HALTED", "auto"),
        ("ACTIVE", "operator"),
    ]
    for state, cause in steps:
        result = await repo.transition(state, cause=cause, actor="test", reason=f"{state}/{cause}")
        assert result.changed
    history = await repo.history(limit=20)
    assert [(h.state, h.cause) for h in reversed(history)] == steps
    assert [h.id for h in history] == sorted((h.id for h in history), reverse=True)


@pytest.mark.asyncio
async def test_scopes_are_independent(scope) -> None:
    factory, account = scope
    other = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=other, venue="bitfinex", label="other"))
    await _repo(factory, account, "ci").transition("HALTED", cause="operator", actor="t",
                                                   reason="stop ci")
    assert await _repo(factory, account, "prod").current() is None
    assert await _repo(factory, other, "ci").current() is None
    # A HALTED elsewhere does not constrain this scope's first decision.
    await _repo(factory, account, "prod").transition("ACTIVE", cause="operator", actor="t",
                                                     reason="start prod")
    await _repo(factory, account, "prod").transition("HALTED", cause="auto", actor="t",
                                                     reason="stop prod")
