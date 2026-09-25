"""Release flow by change class, approvals, resume and probation (ADR D1-D4).

SQLite and PostgreSQL (``capital_db``). Only the operator authority check and
the FX observation are fakes; the gate, worker, trading state, approvals and
capital reads are the real ones.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from bfx_funding_bot.modules.deployments.tables import DeploymentRow
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.safety.tables import (
    DeploymentApprovalRow,
    TradingControlRequestRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import (
    IllegalTradingTransition,
    Probation,
    TradingStateRepository,
)
from bfx_funding_bot.modules.execution.trading_control import (
    BAKE_MS,
    DeploymentIdentity,
    TradingControlWorker,
    apply_deploy_gate,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from bfx_funding_bot.modules.observability import alerts
from tests.external.bitfinex.test_funding_rules import FixedRules

from .test_capital_command_boundary import boundary, second_ready
from .test_capital_repository import capital_db as capital_db
from .test_capital_repository import capital_engine as capital_engine
from .test_capital_repository import intent

D = Decimal
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
REV = "c" * 40
T0 = 10_000_000


def identity(klass: str = "material", digest: str = DIGEST) -> DeploymentIdentity:
    return DeploymentIdentity(digest, REV, klass)


async def allow(session, *, account_id, user) -> bool:
    return user == "operator"


def worker(factory, account, ident, *, now, rules=True) -> TradingControlWorker:
    return TradingControlWorker(session_factory=factory, account_id=account, environment="ci",
        identity=ident, symbols={"fUST"}, funding_rules=FixedRules(clock=lambda: now()) if rules else None,
        authority=allow, clock=lambda: now())


async def request(factory, account, action, *, digest=DIGEST, by="operator", at=T0):
    request_id = uuid4()
    async with factory.begin() as session:
        await session.execute(insert(TradingControlRequestRow).values(
            request_id=request_id, exchange_account_id=account, deployment_environment="ci",
            action=action, backend_digest=digest if action in {"approve", "resume"} else None,
            reason=f"{action} for test", requested_by=by, created_at_ms=at))
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


# ------------------------------------------------------------------ boot gate


@pytest.mark.asyncio
@pytest.mark.parametrize(("start_state", "start_cause", "ident", "expected", "action"), [
    ("ACTIVE", "operator", identity("material"), ("REDUCING", "material_deploy"), "reducing"),
    ("ACTIVE", "operator", identity("standard"), ("ACTIVE", "operator"), "kept"),
    ("ACTIVE", "operator", DeploymentIdentity.from_env({}), ("REDUCING", "material_deploy"), "reducing"),
    ("ACTIVE", "operator", DeploymentIdentity.from_env(
        {"BFX_IMAGE_DIGEST": DIGEST, "BFX_SOURCE_REVISION": REV, "BFX_CHANGE_CLASS": "cosmetic"}),
     ("REDUCING", "material_deploy"), "reducing"),
    ("REDUCING", "operator", identity("material"), ("REDUCING", "material_deploy"), "reducing"),
    ("HALTED", "auto", identity("material"), ("HALTED", "auto"), "behind_stop"),
])
async def test_boot_gate_by_change_class(capital_db, start_state, start_cause, ident, expected, action):
    factory, account = capital_db
    await start(factory, account, start_state, start_cause)
    decision = await apply_deploy_gate(factory, account_id=account, environment="ci",
                                       identity=ident, now_ms=T0)
    assert decision.action == action
    current = await state_of(factory, account)
    assert (current.state, current.cause) == expected


@pytest.mark.asyncio
async def test_boot_gate_keeps_trading_for_an_approved_material_build(capital_db):
    factory, account = capital_db
    await start(factory, account)
    async with factory.begin() as session:
        session.add(DeploymentApprovalRow(exchange_account_id=account, deployment_environment="ci",
            backend_digest=DIGEST, source_revision=REV, approved_by="operator",
            approved_at_ms=T0, request_id=uuid4()))
    decision = await apply_deploy_gate(factory, account_id=account, environment="ci",
                                       identity=identity("material"), now_ms=T0)
    assert decision.action == "kept" and (await state_of(factory, account)).state == "ACTIVE"


@pytest.mark.asyncio
@pytest.mark.parametrize(("ledger_class", "ledger_revision", "why"), [
    ("material", REV, "ledger_material"),
    ("standard", "d" * 40, "ledger_revision_conflict"),
])
async def test_the_ledger_can_only_raise_the_class(capital_db, ledger_class, ledger_revision, why):
    from datetime import UTC, datetime
    factory, account = capital_db
    await start(factory, account)
    now = datetime.now(UTC)
    async with factory.begin() as session:
        session.add(DeploymentRow(started_at=now, finished_at=now, source_revision=ledger_revision,
            backend_digest=DIGEST, frontend_digest=OTHER, change_class=ledger_class,
            migrations_applied=False, outcome="deployed", detail="fixture"))
    decision = await apply_deploy_gate(factory, account_id=account, environment="ci",
                                       identity=identity("standard"), now_ms=T0)
    assert (decision.change_class, decision.why, decision.action) == ("material", why, "reducing")


# ------------------------------------------------------------ requests


@pytest.mark.asyncio
async def test_approval_moves_a_material_build_to_active_in_probation(capital_db):
    factory, account = capital_db
    await start(factory, account, "REDUCING", "operator")
    await apply_deploy_gate(factory, account_id=account, environment="ci",
                            identity=identity("material"), now_ms=T0)
    request_id = await request(factory, account, "approve")
    assert await worker(factory, account, identity("material"), now=lambda: T0).process(request_id) == "applied"
    current = await state_of(factory, account)
    assert (current.state, current.cause, current.actor) == ("ACTIVE", "operator", "operator")
    # Floor = one venue minimum (150 USD at FX 1) plus the submit margin.
    assert current.probation == Probation(D("0.25"), T0, (("fUST", D("150.75")),))
    async with factory() as session:
        approvals = (await session.scalars(select(DeploymentApprovalRow))).all()
    assert [(a.backend_digest, a.approved_by) for a in approvals] == [(DIGEST, "operator")]
    assert (await outcome(factory, request_id))[0] == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "action", "ident", "by", "code"), [
    ("ACTIVE", "approve", identity("standard"), "operator", "approval_not_required"),
    ("REDUCING", "approve", identity("material", OTHER), "operator", "digest_not_running"),
    ("REDUCING", "approve", identity("material"), "intruder", "operator_not_authorized"),
    ("HALTED", "resume", identity("material"), "operator", "approval_required"),
    ("ACTIVE", "resume", identity("standard"), "operator", "already_active"),
])
async def test_requests_are_refused_with_a_bounded_reason(capital_db, setup, action, ident, by, code):
    factory, account = capital_db
    await start(factory, account, setup, "auto" if setup == "HALTED" else "operator")
    before = await state_of(factory, account)
    request_id = await request(factory, account, action, by=by)
    assert await worker(factory, account, ident, now=lambda: T0).process(request_id) == "rejected"
    assert await outcome(factory, request_id) == ("rejected", code)
    assert await state_of(factory, account) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "cause", "probation"), [
    ("HALTED", "auto", True),         # an automatic protection: prove the system again
    ("HALTED", "operator", False),    # an operator's own stop of a proven build
    ("HALTED", "kill_switch", False),
    ("REDUCING", "operator", False),  # maintenance: nothing new to prove
])
async def test_resume_enters_probation_only_after_an_automatic_stop(capital_db, setup, cause, probation):
    factory, account = capital_db
    await start(factory, account, setup, cause)
    request_id = await request(factory, account, "resume")
    assert await worker(factory, account, identity("standard"), now=lambda: T0).process(request_id) == "applied"
    current = await state_of(factory, account)
    assert current.state == "ACTIVE"
    assert (current.probation is not None) is probation


@pytest.mark.asyncio
async def test_a_material_build_approved_while_halted_still_runs_its_probation(capital_db):
    factory, account = capital_db
    await start(factory, account, "HALTED", "operator")
    w = worker(factory, account, identity("material"), now=lambda: T0)
    assert await w.process(await request(factory, account, "approve")) == "applied"
    assert (await state_of(factory, account)).state == "HALTED"  # a stop stays a stop
    assert await w.process(await request(factory, account, "resume")) == "applied"
    assert (await state_of(factory, account)).probation is not None


@pytest.mark.asyncio
async def test_no_fx_observation_fails_the_request_and_changes_nothing(capital_db):
    factory, account = capital_db
    await start(factory, account, "HALTED", "auto")
    request_id = await request(factory, account, "resume")
    w = worker(factory, account, identity("standard"), now=lambda: T0, rules=False)
    assert await w.process(request_id) == "failed"
    assert await outcome(factory, request_id) == ("failed", "funding_rule_unavailable")
    assert (await state_of(factory, account)).state == "HALTED"


# ----------------------------------------------------------------- probation


async def acknowledged(factory, account, *, at, count):
    for n in range(count):
        _, decision = intent(account, "150", 900 + n + at % 1000)
        async with factory.begin() as session:
            session.add(decision)
            await session.flush()
            session.add(SubmissionAttemptRow(attempt_id=uuid4(), execution_decision_id=decision.decision_id,
                exchange_account_id=account, deployment_environment="ci", symbol="fUST", cid=900 + n,
                normalized_payload={"amount": "150"}, payload_sha256="x", started_at_ms=at,
                completed_at_ms=at, outcome_kind="acknowledged"))


async def probation_state(factory, account, *, started=T0):
    repo = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await repo.transition("ACTIVE", cause="operator", actor="t", reason="approved", now_ms=started,
        probation=Probation.starting(multiplier=D("0.25"), started_at_ms=started, floor={"fUST": D("151.50")}))
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize(("elapsed", "acks", "before_start", "lifted"), [
    (BAKE_MS - 1, 3, 0, False),   # not 24 hours yet
    (BAKE_MS, 2, 0, False),       # too few acknowledged submits
    (BAKE_MS, 2, 5, False),       # acknowledgements before the probation do not count
    (BAKE_MS, 3, 0, True),
])
async def test_probation_lifts_after_24h_and_three_acknowledged_submits(capital_db, elapsed, acks,
                                                                        before_start, lifted):
    factory, account = capital_db
    await probation_state(factory, account)
    await acknowledged(factory, account, at=T0 - 1, count=before_start)
    await acknowledged(factory, account, at=T0 + 1, count=acks)
    result = await worker(factory, account, identity("standard"), now=lambda: T0 + elapsed).lift_probation_if_passed()
    current = await state_of(factory, account)
    assert (result is not None) is lifted
    if lifted:
        assert (current.state, current.cause, current.probation) == ("ACTIVE", "auto", None)
    else:
        assert current.probation is not None


@pytest.mark.asyncio
async def test_an_automatic_halt_during_probation_restarts_it(capital_db):
    factory, account = capital_db
    repo = await probation_state(factory, account)
    await acknowledged(factory, account, at=T0 + 1, count=3)
    await repo.transition("HALTED", cause="auto", actor="auto:loss_limiter", reason="loss", now_ms=T0 + 2)
    resumed_at = T0 + BAKE_MS - 10
    w = worker(factory, account, identity("standard"), now=lambda: resumed_at)
    assert await w.process(await request(factory, account, "resume")) == "applied"
    assert (await state_of(factory, account)).probation.started_at_ms == resumed_at
    # 24 hours after the ORIGINAL start is not enough: the window restarted.
    w_later = worker(factory, account, identity("standard"), now=lambda: T0 + BAKE_MS + 10)
    assert await w_later.lift_probation_if_passed() is None


@pytest.mark.asyncio
async def test_a_transition_the_rules_refuse_is_a_rejection_not_a_fault(capital_db, monkeypatch):
    from bfx_funding_bot.modules.execution import trading_control
    factory, account = capital_db
    await start(factory, account, "REDUCING", "operator")

    async def refuse(*args, **kwargs):
        raise IllegalTradingTransition("illegal trading state transition: by rule")

    monkeypatch.setattr(trading_control, "append_transition", refuse)
    request_id = await request(factory, account, "resume")
    assert await worker(factory, account, identity("standard"), now=lambda: T0).process(request_id) == "rejected"
    assert await outcome(factory, request_id) == (
        "rejected", "illegal_transition: illegal trading state transition: by rule")


@pytest.mark.asyncio
async def test_the_idle_worker_lifts_a_probation_that_has_passed(capital_db):
    factory, account = capital_db
    await probation_state(factory, account)
    await acknowledged(factory, account, at=T0 + 1, count=3)
    assert await worker(factory, account, identity("standard"), now=lambda: T0 + BAKE_MS).tick() is False
    current = await state_of(factory, account)
    assert (current.cause, current.probation) == ("auto", None)


# ------------------------------------------------------------ pause and kill


class KillSpy:
    def __init__(self, factory, account):
        self.factory, self.account, self.calls = factory, account, []

    async def engage(self, *, cause, actor, reason, when_already_halted="retry"):
        # Runs after the request committed, outside every lock: HALTED is
        # already what another reader sees.
        self.calls.append((cause, actor, reason, when_already_halted,
                           (await state_of(self.factory, self.account)).state))


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "cause", "outcome", "expected"), [
    ("ACTIVE", "operator", "applied", ("REDUCING", "operator")),
    ("REDUCING", "operator", "applied", ("REDUCING", "operator")),  # already paused
    ("HALTED", "auto", "rejected", ("HALTED", "auto")),              # a stop is not relabelled a pause
    ("REDUCING", "material_deploy", "rejected", ("REDUCING", "material_deploy")),
])
async def test_pause_is_an_operator_reducing(capital_db, setup, cause, outcome, expected):
    factory, account = capital_db
    await start(factory, account, setup, cause)
    # A stop never waits on the running build or the venue.
    w = worker(factory, account, identity("material", OTHER), now=lambda: T0, rules=False)
    request_id = await request(factory, account, "pause")
    assert await w.process(request_id) == outcome
    current = await state_of(factory, account)
    assert (current.state, current.cause) == expected
    if outcome == "rejected":
        assert (await outcome_of(factory, request_id))[1].startswith("illegal_transition")


async def outcome_of(factory, request_id):
    return await outcome(factory, request_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "cause"), [("ACTIVE", "operator"), ("REDUCING", "material_deploy"),
                                              ("HALTED", "auto")])
async def test_kill_writes_halted_then_runs_the_venue_cancel_all(capital_db, setup, cause):
    factory, account = capital_db
    await start(factory, account, setup, cause)
    w = worker(factory, account, identity("material", OTHER), now=lambda: T0, rules=False)
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
async def test_a_kill_goes_first_and_a_pending_request_never_blocks_it(capital_db):
    factory, account = capital_db
    await start(factory, account, "REDUCING", "operator")
    older = await request(factory, account, "resume", at=T0 - 10)
    kill_id = await request(factory, account, "kill", at=T0)  # its own pending lane
    w = worker(factory, account, identity("standard"), now=lambda: T0, rules=False)
    w.kill_switch = KillSpy(factory, account)
    assert await w.tick() is True
    assert (await outcome(factory, kill_id))[0] == "applied"
    # What was asked before the stop cannot undo it afterwards.
    assert await outcome(factory, older) == ("rejected", "superseded_by_kill")
    assert await w.tick() is False
    assert (await state_of(factory, account)).state == "HALTED"


@pytest.mark.asyncio
async def test_a_kill_without_a_kill_switch_is_halted_and_alerts(capital_db, monkeypatch):
    factory, account = capital_db
    await start(factory, account)
    sent = []
    monkeypatch.setattr(alerts, "emit", lambda event, *, level=None, **fields: sent.append((event, level, fields)))
    w = worker(factory, account, identity("standard"), now=lambda: T0)
    assert await w.process(await request(factory, account, "kill")) == "applied"
    assert (await state_of(factory, account)).state == "HALTED"
    assert any(event == "trading_control_failed" and level == "critical"
               and "not cancelled" in fields["reason"] for event, level, fields in sent)


# ------------------------------------------- a probation cannot be escaped


async def approved_into_probation(factory, account):
    """A material build approved into its probation through the real worker."""
    await start(factory, account, "REDUCING", "operator")
    await apply_deploy_gate(factory, account_id=account, environment="ci",
                            identity=identity("material"), now_ms=T0)
    w = worker(factory, account, identity("material"), now=lambda: T0)
    assert await w.process(await request(factory, account, "approve")) == "applied"
    probation = (await state_of(factory, account)).probation
    assert probation is not None
    return TradingStateRepository(factory, account_id=account, deployment_environment="ci"), probation


def same_limits(a: Probation, b: Probation) -> bool:
    return (a.multiplier, a.floor) == (b.multiplier, b.floor)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["webapi_resume", "admin_token_resume"])
async def test_a_pause_during_probation_resumes_inside_it(capital_db, path):
    """ADR D3 invariant: exposure stays within the probation until it passes.
    A maintenance pause and resume must not be a way out of it."""
    from tests.modules.admin.test_trading_status import _service
    factory, account = capital_db
    repo, probation = await approved_into_probation(factory, account)
    await repo.transition("REDUCING", cause="operator", actor="t", reason="maintenance", now_ms=T0 + 5)
    if path == "webapi_resume":
        # No venue: re-entering the same limits needs no new minimum.
        w = worker(factory, account, identity("material"), now=lambda: T0 + 10, rules=False)
        assert await w.process(await request(factory, account, "resume")) == "applied"
    else:
        out = await _service(trading_state=repo).resume(reason="maintenance done", actor="admin")
        assert out["probation"] is not None
    current = await state_of(factory, account)
    assert current.state == "ACTIVE" and current.probation is not None
    assert same_limits(current.probation, probation)
    assert current.probation.started_at_ms > T0  # the 24 hours count again


@pytest.mark.asyncio
async def test_an_operator_stop_during_probation_resumes_inside_it(capital_db):
    """A standard build (so no material-build rule applies) in the probation
    that followed an automatic halt; the operator's own kill does not end it."""
    factory, account = capital_db
    repo = await start(factory, account, "HALTED", "auto")
    w = worker(factory, account, identity("standard"), now=lambda: T0)
    assert await w.process(await request(factory, account, "resume")) == "applied"
    probation = (await state_of(factory, account)).probation
    await repo.transition("HALTED", cause="operator", actor="will", reason="venue incident", now_ms=T0 + 5)
    w_later = worker(factory, account, identity("standard"), now=lambda: T0 + 10, rules=False)
    assert await w_later.process(await request(factory, account, "resume")) == "applied"
    current = await state_of(factory, account)
    assert current.probation is not None and same_limits(current.probation, probation)
    assert current.probation.started_at_ms == T0 + 10


@pytest.mark.asyncio
async def test_no_writer_can_leave_an_unfinished_probation(capital_db):
    factory, account = capital_db
    repo = await probation_state(factory, account)
    with pytest.raises(IllegalTradingTransition, match="probation has not passed"):
        await repo.transition("ACTIVE", cause="operator", actor="t", reason="drop it", now_ms=T0 + 1)
    await repo.transition("REDUCING", cause="operator", actor="t", reason="maintenance", now_ms=T0 + 5)
    with pytest.raises(IllegalTradingTransition, match="probation has not passed"):
        await repo.transition("ACTIVE", cause="operator", actor="t", reason="bypass", now_ms=T0 + 6)
    assert (await state_of(factory, account)).state == "REDUCING"


@pytest.mark.asyncio
@pytest.mark.parametrize(("setup", "cause"), [("HALTED", "auto"), ("HALTED", "operator"),
                                              ("REDUCING", "material_deploy")])
async def test_the_admin_token_lifts_only_an_operator_pause(capital_db, setup, cause):
    from bfx_funding_bot.modules.execution.safety.trading_state import NotAnOperatorPause
    factory, account = capital_db
    repo = await start(factory, account, setup, cause)
    before = await state_of(factory, account)
    with pytest.raises(NotAnOperatorPause):
        await repo.resume_pause(actor="admin", reason="static token", now_ms=T0)
    assert await state_of(factory, account) == before


@pytest.mark.asyncio
async def test_after_the_lift_a_pause_resumes_without_probation(capital_db):
    factory, account = capital_db
    repo = await probation_state(factory, account)
    await acknowledged(factory, account, at=T0 + 1, count=3)
    assert await worker(factory, account, identity("standard"),
                        now=lambda: T0 + BAKE_MS).lift_probation_if_passed() is not None
    await repo.transition("REDUCING", cause="operator", actor="t", reason="maintenance", now_ms=T0 + BAKE_MS + 1)
    result = await repo.resume_pause(actor="t", reason="done", now_ms=T0 + BAKE_MS + 2)
    assert (result.state.state, result.state.probation) == ("ACTIVE", None)


@pytest.mark.asyncio
async def test_a_resume_that_starts_no_probation_does_not_need_the_venue(capital_db):
    """Only a transition that starts a probation observes the venue minimum;
    anything else must not depend on the venue (no guard deadlock)."""
    factory, account = capital_db
    await start(factory, account, "REDUCING", "operator")
    w = worker(factory, account, identity("standard"), now=lambda: T0, rules=False)
    assert await w.process(await request(factory, account, "resume")) == "applied"
    current = await state_of(factory, account)
    assert (current.state, current.probation) == ("ACTIVE", None)


@pytest.mark.asyncio
async def test_the_release_flow_alerts_the_operator(capital_db, monkeypatch):
    factory, account = capital_db
    sent: list[tuple[str, str | None, dict]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, *, level=None, **fields:
                        sent.append((event, level, fields)))
    await start(factory, account)
    sent.clear()
    await apply_deploy_gate(factory, account_id=account, environment="ci",
                            identity=identity("material"), now_ms=T0)
    assert [(e, lv, f.get("action")) for e, lv, f in sent if e == "deploy_gate"] == [
        ("deploy_gate", "warning", "reducing")]
    w = worker(factory, account, identity("material"), now=lambda: T0)
    assert await w.process(await request(factory, account, "approve")) == "applied"
    assert await w.process(await request(factory, account, "approve")) == "rejected"
    await acknowledged(factory, account, at=T0 + 1, count=3)
    assert await worker(factory, account, identity("material"),
                        now=lambda: T0 + BAKE_MS).lift_probation_if_passed() is not None
    repo = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await repo.transition("HALTED", cause="auto", actor="auto:x", reason="x", now_ms=T0 + BAKE_MS + 1)
    no_venue = worker(factory, account, identity("material"), now=lambda: T0 + BAKE_MS + 2, rules=False)
    assert await no_venue.process(await request(factory, account, "resume")) == "failed"
    events = [(e, lv) for e, lv, _ in sent]
    for expected in [("trading_control_applied", "warning"), ("probation_started", "warning"),
                     ("trading_control_rejected", "warning"), ("probation_lifted", "info"),
                     ("trading_control_failed", "critical")]:
        assert expected in events
    changes = [f["state"] for e, _, f in sent if e == "trading_state_changed"]
    assert changes == ["REDUCING", "ACTIVE", "ACTIVE", "HALTED"]  # gate, approval, lift, halt


# -------------------------------------------------------- capital probation


@pytest.mark.asyncio
@pytest.mark.parametrize(("floor", "cell_limit"), [
    ("151.50", "175"),     # 25% of 700 is above one minimum offer
    ("200", "200"),        # the floor: at least one venue-minimum offer
    ("900", "700"),        # never above the normal limit
])
async def test_every_capital_reader_sees_the_probation_limit(capital_db, floor, cell_limit):
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, trading = await boundary(factory, account)
    normal = await runtime.read(symbol="fUST", cell_id="a30")
    assert normal.budget.cell_limit == D("700.00")
    await trading.transition("ACTIVE", cause="operator", actor="t", reason="probation", now_ms=T0,
        probation=Probation.starting(multiplier=D("0.25"), started_at_ms=T0, floor={"fUST": D(floor)}))
    view = await runtime.read(symbol="fUST", cell_id="a30")
    assert view.budget.cell_limit == D(cell_limit)
    assert view.budget.max_new_offer == D(cell_limit)
    # Command admission re-reads the same budget: the 500 planned before is
    # refused whenever the probation limit is below it.
    if D(cell_limit) < D("500"):
        with pytest.raises(CommandGateBlocked):
            await gate.submit(ready, ctx)
        assert venue.received == []
    else:
        await gate.submit(ready, ctx)
        assert len(venue.received) == 1


@pytest.mark.asyncio
async def test_a_submit_within_the_probation_limit_is_admitted(capital_db):
    factory, account = capital_db
    gate, venue, ready, ctx, _, trading = await boundary(factory, account)
    await trading.transition("ACTIVE", cause="operator", actor="t", reason="probation", now_ms=T0,
        probation=Probation.starting(multiplier=D("0.25"), started_at_ms=T0, floor={"fUST": D("151.50")}))
    small = await second_ready(factory, account, ready)
    small = replace(small, decision=small.decision.model_copy(update={"offer_amount_usdt": 160}))
    async with factory.begin() as session:
        from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
        (await session.get(ExecutionDecisionRow, small.decision_id)).amount_usdt = D("160")
    await gate.submit(small, ctx)
    assert len(venue.received) == 1


@pytest.mark.parametrize(("env", "klass", "problem"), [
    ({}, "material", "deployment_identity_missing"),
    ({"BFX_IMAGE_DIGEST": DIGEST, "BFX_SOURCE_REVISION": REV}, "material", "change_class_invalid"),
    ({"BFX_IMAGE_DIGEST": "sha256:bad", "BFX_SOURCE_REVISION": REV, "BFX_CHANGE_CLASS": "standard"},
     "material", "deployment_identity_invalid"),
    ({"BFX_IMAGE_DIGEST": DIGEST, "BFX_SOURCE_REVISION": "short", "BFX_CHANGE_CLASS": "standard"},
     "material", "deployment_identity_invalid"),
    ({"BFX_IMAGE_DIGEST": DIGEST, "BFX_SOURCE_REVISION": REV, "BFX_CHANGE_CLASS": "standard"},
     "standard", None),
])
def test_deploy_identity_fails_closed_to_material(env, klass, problem):
    parsed = DeploymentIdentity.from_env(env)
    assert (parsed.change_class, parsed.problem) == (klass, problem)
