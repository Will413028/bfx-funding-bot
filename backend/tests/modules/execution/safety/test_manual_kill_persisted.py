"""ManualKillGuard reads the durable trading state, and fails closed when it can't.

Two stop mechanisms, both in the same direction (OR): the env flag is
break-glass — it works with no database at all — and the DB row is the durable
decision that survives a deploy reverting canary.env. Unlike the allocation-cap
env/yaml pair that caused 2026-07-27, there is no ambiguity about "which one
binds": either being set means halted, and the status endpoint reports both
separately.

Fail-closed is the whole point of the control: if the halt state cannot be
read, the safe answer is "blocked". A kill switch that opens when the database
hiccups is not a kill switch.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.hard_guards import ManualKillGuard
from bfx_funding_bot.modules.execution.safety.trading_state import TradingState
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _post() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
        symbol="fUST",
    )


class _FakeStore:
    def __init__(self, state: TradingState | None) -> None:
        self._state = state
        self.calls = 0

    async def current(self) -> TradingState | None:
        self.calls += 1
        return self._state


class _BrokenStore:
    async def current(self) -> TradingState | None:
        raise RuntimeError("connection was closed in the middle of operation")


def _state(state: str, cause: str = "operator") -> TradingState:
    return TradingState(
        id=7, state=state, cause=cause, reason="candle distortion", actor="admin",
        created_at_ms=1000,
    )


def _halted(halted: bool) -> TradingState:
    return _state("HALTED" if halted else "ACTIVE")


@pytest.mark.asyncio
async def test_persisted_halt_blocks_with_no_env_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property P2 exists for: canary.env reverted, halt still holds."""
    g = ManualKillGuard(trading_state=_FakeStore(_halted(True)))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "candle distortion" in (r.reason or "")
    assert "admin" in (r.reason or "")


@pytest.mark.asyncio
async def test_persisted_resume_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    g = ManualKillGuard(trading_state=_FakeStore(_halted(False)))
    assert (await g.evaluate(_post(), _ctx())).allowed is True


@pytest.mark.asyncio
async def test_no_persisted_record_blocks_like_halted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: no decision recorded is read as HALTED. A new scope trades
    only after an operator records ACTIVE."""
    g = ManualKillGuard(trading_state=_FakeStore(None))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "no trading state recorded" in (r.reason or "")


@pytest.mark.asyncio
async def test_a_tripped_protection_blocks_before_its_halt_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending: list[str] = []
    g = ManualKillGuard(trading_state=_FakeStore(_halted(False)),
                        pending_stop=lambda: pending[0] if pending else None)
    assert (await g.evaluate(_post(), _ctx())).allowed is True
    pending.append("submit_outcome_unknown: cid=1")
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "submit_outcome_unknown" in (r.reason or "")


@pytest.mark.asyncio
async def test_unreadable_halt_state_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    g = ManualKillGuard(trading_state=_BrokenStore())
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "failing closed" in (r.reason or "")


@pytest.mark.asyncio
async def test_the_retired_env_flag_has_no_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no environment break-glass any more (the container stop is):
    a set BFX_KILL_SWITCH changes nothing, the trading state decides."""
    monkeypatch.setenv("BFX_KILL_SWITCH", "true")
    store = _FakeStore(_halted(False))
    g = ManualKillGuard(trading_state=store)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True
    assert store.calls == 1


@pytest.mark.asyncio
async def test_without_a_store_only_a_tripped_protection_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paper/shadow construct the guard with no store: only a tripped protection blocks."""
    assert (await ManualKillGuard().evaluate(_post(), _ctx())).allowed is True
    tripped = ManualKillGuard(pending_stop=lambda: "writer_lock_lost")
    assert (await tripped.evaluate(_post(), _ctx())).allowed is False
