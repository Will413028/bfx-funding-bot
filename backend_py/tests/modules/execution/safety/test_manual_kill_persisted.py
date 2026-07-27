"""ManualKillGuard reads the PERSISTED halt, and fails closed when it can't.

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
from bfx_funding_bot.modules.execution.safety.halt_state import HaltState
from bfx_funding_bot.modules.execution.safety.hard_guards import ManualKillGuard
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
    def __init__(self, state: HaltState | None) -> None:
        self._state = state
        self.calls = 0

    async def current(self) -> HaltState | None:
        self.calls += 1
        return self._state


class _BrokenStore:
    async def current(self) -> HaltState | None:
        raise RuntimeError("connection was closed in the middle of operation")


def _halted(halted: bool) -> HaltState:
    return HaltState(
        halted=halted, reason="candle distortion", actor="admin",
        created_at_ms=1000, id=7,
    )


@pytest.mark.asyncio
async def test_persisted_halt_blocks_with_no_env_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property P2 exists for: canary.env reverted, halt still holds."""
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    g = ManualKillGuard(halt_store=_FakeStore(_halted(True)))
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "candle distortion" in (r.reason or "")
    assert "admin" in (r.reason or "")


@pytest.mark.asyncio
async def test_persisted_resume_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    g = ManualKillGuard(halt_store=_FakeStore(_halted(False)))
    assert (await g.evaluate(_post(), _ctx())).allowed is True


@pytest.mark.asyncio
async def test_no_persisted_record_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never-configured is not halted — otherwise every fresh environment would
    deadlock on first boot."""
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    g = ManualKillGuard(halt_store=_FakeStore(None))
    assert (await g.evaluate(_post(), _ctx())).allowed is True


@pytest.mark.asyncio
async def test_unreadable_halt_state_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    g = ManualKillGuard(halt_store=_BrokenStore())
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "failing closed" in (r.reason or "")


@pytest.mark.asyncio
async def test_env_flag_blocks_without_consulting_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break-glass must work when the database is the thing that is broken, so
    the env check comes first and short-circuits."""
    monkeypatch.setenv("BFX_KILL_SWITCH", "true")
    store = _FakeStore(_halted(False))
    g = ManualKillGuard(halt_store=store)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "BFX_KILL_SWITCH" in (r.reason or "")
    assert store.calls == 0


@pytest.mark.asyncio
async def test_without_a_store_behaviour_is_the_previous_env_only_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paper/shadow and the existing tests construct the guard with no store."""
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    assert (await ManualKillGuard().evaluate(_post(), _ctx())).allowed is True
    monkeypatch.setenv("BFX_KILL_SWITCH", "true")
    assert (await ManualKillGuard().evaluate(_post(), _ctx())).allowed is False
