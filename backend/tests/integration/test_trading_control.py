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


async def written_by(factory, state_id):
    """The operator request a trading state row names, if any."""
    from bfx_funding_bot.modules.execution.safety.tables import TradingStateRow

    async with factory() as session:
        return (await session.get(TradingStateRow, state_id)).operator_request_id


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
    assert await written_by(factory, current.id) == request_id


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
    def __init__(self, factory, account, covers=("UST",)):
        self.factory, self.account, self.calls, self.requests = factory, account, [], []
        self.covers = covers

    async def currencies(self):
        return self.covers, None

    async def engage(self, *, cause, actor, reason, when_already_halted="retry",
                     operator_request_id=None):
        # Runs after the request committed, outside every lock: HALTED is
        # already what another reader sees.
        self.calls.append((cause, actor, reason, when_already_halted,
                           (await state_of(self.factory, self.account)).state))
        self.requests.append(operator_request_id)


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
    # A kill over an automatic halt makes it the operator's, which never resumes
    # automatically (ADR 2026-09-26 auto-halt-resumes-when-condition-clears).
    assert (current.state, current.cause, current.actor) == ("HALTED", "operator", "operator")
    # Asking again is how an incomplete cancel-all is retried, as /admin/halt.
    assert kill.calls == [("operator", "operator", "kill: kill for test", "retry", "HALTED")]
    # The HALTED it wrote, and the cancel-all it runs, name the request.
    assert await written_by(factory, current.id) == request_id
    assert kill.requests == [request_id]


@pytest.mark.asyncio
async def test_a_kill_that_restates_a_halt_names_only_its_cancel_all(migrated_db):
    """Over an operator HALTED the kill writes nothing: the row in force keeps naming whoever
    wrote it, and the retried cancel-all names this request. The request row never records
    an effect (its product column is closed)."""
    factory, account = migrated_db
    await start(factory, account, "HALTED", "operator")
    before = await state_of(factory, account)
    w = worker(factory, account)
    w.kill_switch = kill = KillSpy(factory, account)
    request_id = await request(factory, account, "kill")
    statements: list[str] = []
    from sqlalchemy import event

    sync_engine = factory.kw["bind"].sync_engine

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        assert await w.process(request_id) == "applied"
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)
    assert await state_of(factory, account) == before
    assert await written_by(factory, before.id) is None
    assert kill.requests == [request_id]
    updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE TRADING_CONTROL_REQUESTS")]
    assert updates and not any("trading_state_id" in s for s in updates)


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


# ------------------------------------------------- a kill cut off after commit


async def cut_off_kill(factory, account, w, *, at=T0, covers=("UST",)):
    """Apply a kill whose cancel-all never runs: the process stopped after the commit."""
    request_id = await request(factory, account, "kill", at=at)
    w.kill_switch = None
    assert await w.process(request_id) == "applied"
    w.kill_switch = KillSpy(factory, account, covers)
    return request_id


async def audit(factory, account, *phases, currency="UST", at=T0):
    """Cancel-all audit rows of one attempt (/admin/halt's: no request named)."""
    from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow

    current = await state_of(factory, account)
    attempt = uuid4()
    async with factory.begin() as session:
        for phase in phases:
            session.add(FundingCancelAllAuditRow(
                exchange_account_id=account, deployment_environment="ci",
                trading_state_id=current.id, attempt_id=attempt, currency=currency, phase=phase,
                actor="operator", occurred_at_ms=at))


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", [("ACTIVE", "operator"), ("HALTED", "operator")])
async def test_an_idle_worker_runs_the_cancel_all_a_kill_never_finished(migrated_db, monkeypatch,
                                                                        setup):
    """Over an operator HALTED the kill writes no state row: the request is the work item."""
    factory, account = migrated_db
    await start(factory, account, *setup)
    sent = []
    monkeypatch.setattr(alerts, "emit", lambda event, *, level=None, **fields:
                        sent.append((event, level)))
    w = worker(factory, account)
    request_id = await cut_off_kill(factory, account, w)
    await w.idle()
    assert w.kill_switch.calls == [("operator", "operator", "kill: kill for test", "retry", "HALTED")]
    assert w.kill_switch.requests == [request_id]
    assert ("kill_cancel_all_caught_up", "critical") in sent
    # Once per process: an audit that cannot be written does not loop the venue.
    await w.idle()
    assert w.kill_switch.requests == [request_id]
    # A restarted process tries again.
    again = worker(factory, account)
    again.kill_switch = KillSpy(factory, account)
    await again.idle()
    assert again.kill_switch.requests == [request_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(("phases", "caught_up"), [
    ([("acknowledged",)], False),
    ([("requested", "failed")], False),        # finished; asking again retries it
    ([("skipped",)], False),
    ([("requested",)], True),                  # the venue call was cut off
    ([("requested", "acknowledged"), ("requested",)], True),
])
async def test_a_finished_cancel_all_since_the_kill_is_left_alone(migrated_db, phases, caught_up):
    factory, account = migrated_db
    await start(factory, account)
    w = worker(factory, account)
    await cut_off_kill(factory, account, w)
    for attempt in phases:
        await audit(factory, account, *attempt)
    await w.idle()
    assert bool(w.kill_switch.requests) is caught_up


@pytest.mark.asyncio
async def test_a_cancel_all_before_the_kill_does_not_count(migrated_db):
    factory, account = migrated_db
    await start(factory, account, "HALTED", "operator")
    await audit(factory, account, "acknowledged", at=T0 - 1)
    w = worker(factory, account)
    request_id = await cut_off_kill(factory, account, w)
    await w.idle()
    assert w.kill_switch.requests == [request_id]


@pytest.mark.asyncio
async def test_a_kill_resumed_since_is_never_caught_up(migrated_db):
    """After a resume, a later HALTED is someone else's stop: an automatic one writes HALTED
    alone, and the planner pulls the managed offers."""
    factory, account = migrated_db
    await start(factory, account)
    w = worker(factory, account, now=lambda: clock[0])
    clock = [T0]
    await cut_off_kill(factory, account, w)
    clock[0] = T0 + 1
    assert await w.process(await request(factory, account, "resume", at=T0 + 1)) == "applied"
    await w.idle()       # ACTIVE: nothing to do
    repo = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await repo.transition("HALTED", cause="auto", actor="auto:x", reason="x", now_ms=T0 + 2)
    await w.idle()
    assert w.kill_switch.requests == []


@pytest.mark.asyncio
async def test_without_an_applied_kill_an_idle_worker_does_nothing(migrated_db):
    factory, account = migrated_db
    await start(factory, account, "HALTED", "auto")
    w = worker(factory, account)
    w.kill_switch = kill = KillSpy(factory, account)
    await w.idle()
    assert kill.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", [("ACTIVE", "operator"), ("HALTED", "operator")])
async def test_a_resume_in_the_kills_own_millisecond_still_ends_it(migrated_db, setup):
    """The clock does not order a kill and a resume; the trading state's ids do."""
    factory, account = migrated_db
    await start(factory, account, *setup)
    w = worker(factory, account)
    await cut_off_kill(factory, account, w)
    assert await w.process(await request(factory, account, "resume")) == "applied"
    await w.idle()       # ACTIVE: nothing to do
    repo = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await repo.transition("HALTED", cause="auto", actor="auto:x", reason="x", now_ms=T0)
    await w.idle()
    assert w.kill_switch.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("covers", "caught_up"), [
    (("UST", "USD"), True),    # cut off before UST's first row
    (("USD",), False),
    ((), False),               # nothing to cancel: an empty cancel-all writes no row
])
async def test_every_currency_the_cancel_all_covers_must_have_finished(migrated_db, covers,
                                                                        caught_up):
    factory, account = migrated_db
    await start(factory, account)
    w = worker(factory, account)
    await cut_off_kill(factory, account, w, covers=covers)
    if covers:
        await audit(factory, account, "requested", "acknowledged", currency="USD")
    await w.idle()
    assert bool(w.kill_switch.requests) is caught_up
