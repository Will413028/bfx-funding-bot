"""Operator trading control: resume and kill requests (lending envelope ADR D4).

On the migrated PostgreSQL schema, whose triggers are the rules' authority.
Only the operator authority check and the kill switch are fakes; the worker,
trading state and requests are the real ones.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import insert

from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from bfx_funding_bot.modules.execution.safety.trading_state import (
    IllegalTradingTransition,
    TradingStateRepository,
)
from bfx_funding_bot.modules.execution.trading_control import TradingControlWorker
from bfx_funding_bot.modules.observability import alerts

pytestmark = pytest.mark.integration

T0 = 10_000_000


async def allow(session, *, account_id, user) -> bool:
    return user == "operator"


def worker(factory, account, *, now=lambda: T0) -> TradingControlWorker:
    return TradingControlWorker(session_factory=factory, account_id=account, environment="ci",
                                authority=allow, clock=lambda: now())


async def request(factory, account, action, *, by="operator", at=T0):
    request_id = uuid4()
    async with factory.begin() as session:
        await session.execute(insert(TradingControlRequestRow).values(
            request_id=request_id, exchange_account_id=account, deployment_environment="ci",
            action=action, reason=f"{action} for test", requested_by=by, created_at_ms=at))
    return request_id


async def outcome(factory, request_id):
    async with factory() as session:
        row = await session.get(TradingControlRequestRow, request_id)
        return row.state, row.outcome_reason


async def state_of(factory, account):
    return await TradingStateRepository(factory, account_id=account, deployment_environment="ci").current()


async def start(factory, account, state="ACTIVE", cause="operator"):
    repo = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    if state != "ACTIVE":
        await repo.transition("ACTIVE", cause="operator", actor="t", reason="setup", now_ms=1)
    await repo.transition(state, cause=cause, actor="t", reason="setup", now_ms=2)
    return repo


# ------------------------------------------------------------------ resume


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "cause"), [
    ("HALTED", "operator"),
    ("HALTED", "auto"),     # an automatic stop ends the same way: no probation any more
    (None, None),           # no decision ever recorded reads as HALTED
])
async def test_resume_ends_any_halt(migrated_db, setup, cause):
    factory, account = migrated_db
    if setup is not None:
        await start(factory, account, setup, cause)
    request_id = await request(factory, account, "resume")
    assert await worker(factory, account).process(request_id) == "applied"
    current = await state_of(factory, account)
    assert (current.state, current.cause, current.actor) == ("ACTIVE", "operator", "operator")
    assert current.reason == "resumed: resume for test"
    assert await outcome(factory, request_id) == ("applied", "resumed")


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "by", "code"), [
    ("ACTIVE", "operator", "already_active"),
    ("HALTED", "someone", "operator_not_authorized"),
])
async def test_requests_are_refused_with_a_bounded_reason(migrated_db, setup, by, code):
    factory, account = migrated_db
    await start(factory, account, setup)
    before = await state_of(factory, account)
    request_id = await request(factory, account, "resume", by=by)
    assert await worker(factory, account).process(request_id) == "rejected"
    assert (await outcome(factory, request_id))[1].startswith(code)
    assert await state_of(factory, account) == before


@pytest.mark.asyncio
async def test_a_transition_the_rules_refuse_is_a_rejection_not_a_fault(migrated_db, monkeypatch):
    from bfx_funding_bot.modules.execution import trading_control
    factory, account = migrated_db
    await start(factory, account, "HALTED", "operator")

    async def refuse(*args, **kwargs):
        raise IllegalTradingTransition("illegal trading state transition: by rule")

    monkeypatch.setattr(trading_control, "append_transition", refuse)
    request_id = await request(factory, account, "resume")
    assert await worker(factory, account).process(request_id) == "rejected"
    assert await outcome(factory, request_id) == (
        "rejected", "illegal_transition: illegal trading state transition: by rule")


# -------------------------------------------------------------------- kill


class KillSpy:
    def __init__(self, factory, account):
        self.factory, self.account, self.calls = factory, account, []

    async def engage(self, *, cause, actor, reason, when_already_halted="retry"):
        # Runs after the request committed, outside every lock: HALTED is
        # already what another reader sees.
        self.calls.append((cause, actor, reason, when_already_halted,
                           (await state_of(self.factory, self.account)).state))


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "cause"), [("ACTIVE", "operator"), ("HALTED", "auto")])
async def test_kill_writes_halted_then_runs_the_venue_cancel_all(migrated_db, setup, cause):
    factory, account = migrated_db
    await start(factory, account, setup, cause)
    w = worker(factory, account)
    w.kill_switch = kill = KillSpy(factory, account)
    request_id = await request(factory, account, "kill")
    assert await w.process(request_id) == "applied"
    current = await state_of(factory, account)
    if setup == "HALTED":
        assert (current.state, current.cause) == ("HALTED", "auto")  # a stop stays the stop it was
    else:
        assert (current.state, current.cause, current.actor) == ("HALTED", "operator", "operator")
    # Asking again is how an incomplete cancel-all is retried, as /admin/halt.
    assert kill.calls == [("operator", "operator", "kill: kill for test", "retry", "HALTED")]


@pytest.mark.asyncio
async def test_a_kill_goes_first_and_a_pending_request_never_blocks_it(migrated_db):
    factory, account = migrated_db
    await start(factory, account, "HALTED", "auto")
    older = await request(factory, account, "resume", at=T0 - 10)
    kill_id = await request(factory, account, "kill", at=T0)  # its own pending lane
    w = worker(factory, account)
    w.kill_switch = KillSpy(factory, account)
    assert await w.tick() is True
    assert (await outcome(factory, kill_id))[0] == "applied"
    # What was asked before the stop cannot undo it afterwards.
    assert await outcome(factory, older) == ("rejected", "superseded_by_kill")
    assert await w.tick() is False
    assert (await state_of(factory, account)).state == "HALTED"


@pytest.mark.asyncio
async def test_a_kill_without_a_kill_switch_is_halted_and_alerts(migrated_db, monkeypatch):
    factory, account = migrated_db
    await start(factory, account)
    sent = []
    monkeypatch.setattr(alerts, "emit", lambda event, *, level=None, **fields: sent.append((event, level, fields)))
    assert await worker(factory, account).process(await request(factory, account, "kill")) == "applied"
    assert (await state_of(factory, account)).state == "HALTED"
    assert any(event == "trading_control_failed" and level == "critical"
               and "not cancelled" in fields["reason"] for event, level, fields in sent)


@pytest.mark.asyncio
async def test_control_requests_alert_the_operator(migrated_db, monkeypatch):
    factory, account = migrated_db
    sent: list[tuple[str, str | None, dict]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, *, level=None, **fields:
                        sent.append((event, level, fields)))
    await start(factory, account, "HALTED", "auto")
    sent.clear()
    w = worker(factory, account)
    w.kill_switch = KillSpy(factory, account)
    assert await w.process(await request(factory, account, "resume")) == "applied"
    assert await w.process(await request(factory, account, "resume")) == "rejected"
    assert await w.process(await request(factory, account, "kill")) == "applied"
    events = [(e, lv) for e, lv, _ in sent]
    for expected in [("trading_control_applied", "warning"), ("trading_control_rejected", "warning")]:
        assert expected in events
    changes = [f["state"] for e, _, f in sent if e == "trading_state_changed"]
    assert changes == ["ACTIVE", "HALTED"]
