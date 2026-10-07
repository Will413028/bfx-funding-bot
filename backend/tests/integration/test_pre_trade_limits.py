"""Pre-trade limits against migrated PostgreSQL over the ledger; only the venue is fake.

- offer_envelope: an applied policy without an envelope refuses; the operator
  amendment (dry run -> digest -> apply) writes schema 3 through apply_policy;
  the guard then bounds the offer inside the command gate's locked boundary,
  and its open-offer count reads the ledger's live offer mirror.
- The command gate's throttle refuses before anything durable and trips
  HALTED/auto on sustained excess.
- The NAV-drop alert's 24h window survives a restart.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text

from bfx_funding_bot.core.errors import ExecutorTransientError
from bfx_funding_bot.modules.accounts.capital_amendment import (
    PolicyChanges,
    amend_capital_policy,
)
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import ReconcileNavTracker
from bfx_funding_bot.modules.execution.safety.nav_window_store import NavWindowStore
from bfx_funding_bot.modules.execution.safety.pre_trade import (
    CommandThrottle,
    OfferEnvelopeGuard,
)
from bfx_funding_bot.modules.execution.safety.protection import (
    COMMAND_RATE_EXCEEDED,
    NAV_DROP,
    NavDropMonitor,
)
from bfx_funding_bot.modules.execution.safety.tables import NavWindowSampleRow
from bfx_funding_bot.modules.ledger import PolicyRefused
from bfx_funding_bot.modules.ledger.wiring import build_policy_store
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from tests.modules.execution.safety.test_pre_trade import Book, book
from tests.pg_templates import alembic

from .contracts.stacks import ACCOUNT, NOW, SCOPE, Stack, Venue
from .test_capital_command_boundary import (
    attempts,
    boundary,
    clock_revision,
    gate_stack,  # noqa: F401 - fixture re-export
    place_managed,
    planner_ports,
    read_capital,
    second_ready,
    stop_chain,
)
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration


class Trips:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.calls.append((trigger, detail))


@pytest.fixture
def alert_sink() -> Any:
    sink = alerts.AlertSink(None, context="ci")
    previous = alerts.install(sink)
    yield sink
    alerts.install(previous)


FULL = PolicyChanges(max_offer_amount=Decimal("200"), min_period_days=2, max_period_days=2,
                     max_open_offers=2, rate_floor_ratio=Decimal("0.5"),
                     min_rate_apr=Decimal("0.01"))
MARKET = Book(book((0.0003, 2), (0.0001, 2), (0.0002, 2)))  # relative floor 0.0001


def _envelope_guard(stack: Stack) -> OfferEnvelopeGuard:
    return OfferEnvelopeGuard(authority=stack.capital, offers=stack.offers, scope=SCOPE,
                              session_factory=stack.factory, book=MARKET, clock=lambda: NOW)


async def _amend(stack: Stack, changes: PolicyChanges, *,
                 digest: str | None = None) -> dict[str, Any]:
    async with stack.factory() as session:
        report = await amend_capital_policy(session, store=build_policy_store(SCOPE),
                                            scope_lock=stack.lock,
                                            symbol="fUST", changes=changes, apply_digest=digest)
        if report["status"] == "applied":
            await session.commit()
        else:
            await session.rollback()
    return report


@pytest.mark.asyncio
async def test_policy_without_an_envelope_refuses_and_the_amendment_bounds_each_offer(gate_stack):  # noqa: F811
    rig = await boundary(gate_stack)
    gate, venue, ready, ctx, halt = rig.gate, rig.venue, rig.ready, rig.ctx, rig.halt
    guard = _envelope_guard(gate_stack)
    gate._safety_evaluator = stop_chain(halt, guard)

    # Schema 1 (no envelope): refused inside the locked boundary, nothing durable.
    with pytest.raises(CommandGateBlocked, match="envelope_unset"):
        await gate.submit(ready, ctx)
    assert venue.received == [] and await attempts(gate_stack) == 0

    # A first envelope needs every field; the dry run changes nothing; a stale
    # digest is refused.
    with pytest.raises(PolicyRefused, match="envelope_incomplete: max_period_days"):
        await _amend(gate_stack, PolicyChanges(min_period_days=2))
    report = await _amend(gate_stack, FULL)
    assert report["status"] == "dry_run" and report["expected_revision"] == 1
    assert report["new_schema_version"] == 3
    assert report["new_policy"]["envelope"]["min_rate_apr"] == "0.01"
    with pytest.raises(PolicyRefused, match="amendment_changed"):
        await _amend(gate_stack, FULL, digest="0" * 64)
    applied = await _amend(gate_stack, FULL, digest=report["amendment_digest"])
    assert (applied["status"], applied["new_revision"]) == ("applied", 2)
    assert (await _amend(gate_stack, FULL))["status"] == "unchanged"

    view = await read_capital(gate_stack, "a30")
    with pytest.raises(CommandGateBlocked, match=r"offer_amount 499\.99990500 > max_offer_amount 200"):
        await gate.submit(replace(ready, capital_view=view), ctx)
    assert venue.received == [] and await attempts(gate_stack) == 0
    within = replace(await second_ready(rig), capital_view=view)
    await gate.submit(within, ctx)                          # 200 <= 200, rate on the floor
    assert len(venue.received) == 1 and await attempts(gate_stack) == 1

    # A later amendment may change one field, and a bad value is refused.
    one = await _amend(gate_stack, PolicyChanges(max_open_offers=1))
    assert one["new_policy"]["envelope"]["max_open_offers"] == 1
    with pytest.raises(PolicyRefused, match="invalid_policy"):
        await _amend(gate_stack, PolicyChanges(max_period_days=121))


@pytest.mark.asyncio
async def test_disabling_through_the_amendment_zeroes_the_budget(gate_stack):  # noqa: F811
    await boundary(gate_stack)
    report = await _amend(gate_stack, PolicyChanges(enabled=False))
    await _amend(gate_stack, PolicyChanges(enabled=False),
                 digest=report["amendment_digest"])
    view = await read_capital(gate_stack, "a30")
    assert (view.budget.max_new_offer, view.budget.reason) == (Decimal("0"), "policy_disabled")


@pytest.mark.asyncio
async def test_open_offers_are_counted_from_the_live_mirror(gate_stack):  # noqa: F811
    await boundary(gate_stack)
    report = await _amend(gate_stack, FULL)
    await _amend(gate_stack, FULL, digest=report["amendment_digest"])
    guard = _envelope_guard(gate_stack)
    decision = DecisionPayload(decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
                               offer_rate=0.0002, offer_amount_usdt=200, offer_duration_days=2,
                               symbol="fUST")
    ctx = AccountContext(str(ACCOUNT), Credentials("k", "s"), Decimal("0"))
    for offer_id, symbol in (("1", "fUST"), ("2", "fUST"), ("3", "fUSD"), ("4", "fUST")):
        await gate_stack.place(offer_id, "200", symbol=symbol)
    live = [Venue("1", "200"),
            Venue("m", "200"),                  # a manual offer takes no slot (D2)
            Venue("3", "200", symbol="fUSD")]   # another symbol
    # "2" is not live: it left the venue (executed/cancelled), so it is not open.
    await gate_stack.snapshot("1000", offers=tuple(live))
    assert (await guard.evaluate(decision, ctx)).allowed
    await gate_stack.snapshot("1000", offers=(*live, Venue("4", "200")))
    result = await guard.evaluate(decision, ctx)
    assert (result.allowed, result.reason) == (False, "open_offers 2 >= limit 2")


@pytest.mark.asyncio
async def test_throttle_refuses_before_anything_durable_and_trips_on_sustained_excess(
    gate_stack, alert_sink,  # noqa: F811
):
    rig = await boundary(gate_stack)
    gate, venue, ready, ctx = rig.gate, rig.venue, rig.ready, rig.ctx
    trips = Trips()
    gate.throttle = CommandThrottle(capacity=2, refill_per_second=0.0001, trip_blocks=2,
                                    trip_window_s=300, protection=trips)
    await gate.submit(ready, ctx)                                          # token 1
    await gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                      account_id=ctx.account_id, ctx=ctx)                  # token 2
    received = list(venue.received)
    durable = (await attempts(gate_stack), await clock_revision(gate_stack))

    later = await second_ready(rig)
    with pytest.raises(CommandGateBlocked, match="command_rate_limited"):
        await gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                          account_id=ctx.account_id, ctx=ctx)
    assert trips.calls == []  # a refused cancel waits for a token; it never counts toward a stop
    for _ in range(2):
        with pytest.raises(CommandGateBlocked, match="command_rate_limited"):
            await gate.submit(later, ctx)
    assert venue.received == received
    assert (await attempts(gate_stack), await clock_revision(gate_stack)) == durable
    assert [trigger for trigger, _ in trips.calls] == [COMMAND_RATE_EXCEEDED]
    assert alert_sink.counts.get("log_only", 0) >= 2


def _reconciled(account, nav: str, at_ms: int) -> PositionReconciled:
    return PositionReconciled(symbol="fUST", account_id=str(account), n_offers=0, n_credits=0,
                              occurred_at_ms=at_ms, reserved=Decimal("0"), realized=Decimal("0"),
                              available=Decimal(nav))


@pytest.mark.asyncio
async def test_loss_window_survives_a_restart_and_the_nav_drop_still_alerts(gate_stack):  # noqa: F811
    factory, account = gate_stack.factory, ACCOUNT
    store = NavWindowStore(factory, account_id=account, deployment_environment="ci")
    hour = 3_600_000
    before = ReconcileNavTracker(str(account), window_store=store)
    for nav, at in (("1000", 1 * hour), ("1000", 1 * hour + 60_000), ("900", 2 * hour)):
        await before.on_position_reconciled(_reconciled(account, nav, at))
    assert before.realized_loss_pct_24h("fUST") == pytest.approx(10.0)
    async with factory() as session:          # unchanged NAV inside 10 min is not re-written
        assert len((await session.scalars(select(NavWindowSampleRow))).all()) == 2

    forgetful = ReconcileNavTracker(str(account))              # the pre-T9 behaviour
    await forgetful.on_position_reconciled(_reconciled(account, "900", 3 * hour))
    assert forgetful.realized_loss_pct_24h("fUST") == 0.0

    after = ReconcileNavTracker(str(account), window_store=store)
    await after.load_persisted_window()
    assert after.realized_loss_pct_24h("fUST") == pytest.approx(10.0)
    sent: list[str] = []
    monitor = NavDropMonitor(source=after, realized_loss_threshold_pct=5.0,
                             drawdown_threshold_pct=None)
    import bfx_funding_bot.modules.execution.safety.protection as protection_module
    original = protection_module.alerts.emit
    protection_module.alerts.emit = lambda event, **fields: sent.append(event)  # type: ignore[assignment]
    try:
        await monitor.on_position_reconciled(_reconciled(account, "900", 3 * hour))
    finally:
        protection_module.alerts.emit = original  # type: ignore[assignment]
    assert sent == [NAV_DROP]  # an alert, never a stop (lending envelope D3)

    # A day later the loss has aged out of the window, and old rows are pruned.
    await after.on_position_reconciled(_reconciled(account, "900", 3 * hour + 50 * hour))
    assert after.realized_loss_pct_24h("fUST") == 0.0
    async with factory() as session:
        remaining = (await session.scalars(select(NavWindowSampleRow.occurred_at_ms))).all()
    assert min(remaining) >= 3 * hour


@pytest.mark.asyncio
async def test_window_store_failure_never_breaks_the_reconcile_path(gate_stack, monkeypatch):  # noqa: F811
    from bfx_funding_bot.modules.execution.safety import nav_pnl_source

    account = ACCOUNT
    logged: list[str] = []
    # Recorded directly: other tests may disable logging process-wide.
    monkeypatch.setattr(nav_pnl_source.log, "exception",
                        lambda message, *args: logged.append(message % args if args else message))

    class Broken:
        async def load(self) -> dict[str, list[tuple[int, Decimal]]]:
            raise RuntimeError("db down")

        async def save(self, symbol: str, occurred_at_ms: int, nav: Decimal) -> None:
            raise RuntimeError("db down")

    tracker = ReconcileNavTracker(str(account), window_store=Broken())
    await tracker.load_persisted_window()
    await tracker.on_position_reconciled(_reconciled(account, "1000", 1))
    assert tracker.realized_loss_pct_24h("fUST") == 0.0
    assert [line.split(" ")[0] for line in logged] == ["nav_window_load_failed",
                                                       "nav_window_save_failed"]


@pytest.mark.integration
def test_migration_grants_the_runtime_role_only_read_insert_and_prune(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("empty_database", lambda _url: None))
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                                 "rolname='bfx_bot') THEN CREATE ROLE bfx_bot; END IF; END $$")
            conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_bot")
        alembic(url, "upgrade", "head")
        alembic(url, "check")
        with engine.connect() as conn:
            for privilege, expected in (("SELECT", True), ("INSERT", True), ("DELETE", True),
                                        ("UPDATE", False), ("TRUNCATE", False)):
                assert conn.scalar(text("SELECT has_table_privilege('bfx_bot', "
                                        "'nav_window_samples', :p)"), {"p": privilege}) is expected
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM bfx_bot")
        engine.dispose()


class Canceller:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.cancelled: list[str] = []
        self.fail = fail or set()

    async def cancel(self, *, venue_offer_id, signal_correlation_id, account_id, ctx):
        if venue_offer_id in self.fail:
            raise ExecutorTransientError("venue unavailable")
        self.cancelled.append(venue_offer_id)


async def _offer_rows(stack: Stack) -> None:
    """Managed 101 (fUST) and 102 (fUSD), foreign 555 (manual, fUST) live at the venue; the
    managed 103 left it (terminal)."""
    for offer_id, symbol in (("101", "fUST"), ("102", "fUSD"), ("103", "fUST")):
        await place_managed(stack, offer_id, "200", symbol=symbol)
    await stack.snapshot("1000", offers=(
        Venue("101", "200"), Venue("102", "200", symbol="fUSD"), Venue("555", "200")))


@pytest.mark.asyncio
async def test_disabling_a_currency_pulls_only_its_managed_offers(gate_stack):  # noqa: F811
    """D4: the everyday per-currency stop. The planner skips the symbol and the
    managed offers of that symbol are cancelled by id; a foreign offer, another
    currency and an enabled currency are untouched."""
    from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
    from bfx_funding_bot.modules.execution.managed_cancel import ManagedOfferSweep

    rig = await boundary(gate_stack)
    ctx = rig.ctx
    await _offer_rows(gate_stack)   # managed 101 (fUST), 102 (fUSD); foreign 555 (fUST)
    canceller = Canceller()
    planner = object.__new__(DeploymentReconciler)
    planner._conflict_alerted = set()
    for name, port in planner_ports(gate_stack).items():
        setattr(planner, f"_{name}", port)
    planner._managed_sweep = ManagedOfferSweep(
        session_factory=gate_stack.factory, account_id=ACCOUNT, environment="ci",
        canceller=canceller, ctx=ctx, offers=gate_stack.offers)

    assert await planner._pull_if_stopped("fUST") is False    # enabled, ACTIVE: nothing happens
    assert canceller.cancelled == []
    report = await _amend(gate_stack, PolicyChanges(enabled=False))
    await _amend(gate_stack, PolicyChanges(enabled=False), digest=report["amendment_digest"])
    assert await planner._pull_if_stopped("fUST") is True
    assert canceller.cancelled == ["101"]
    # No policy at all is not "disabled": the capital read fails closed on it instead.
    assert await planner._pull_if_stopped("fBTC") is False


@pytest.mark.asyncio
async def test_a_halt_pulls_managed_offers_every_tick_until_none_is_left(gate_stack):  # noqa: F811
    """D3 level 3 is level-triggered: an automatic HALTED writes the state alone,
    and each planner tick cancels whatever managed offer is still open -- a
    cancel that failed on one tick is retried on the next. Foreign offers stay."""
    from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
    from bfx_funding_bot.modules.execution.managed_cancel import ManagedOfferSweep

    rig = await boundary(gate_stack)
    ctx, halt = rig.ctx, rig.halt
    await _offer_rows(gate_stack)
    await halt.transition("HALTED", cause="auto", actor="auto:identity_conflict", reason="x")
    canceller = Canceller(fail={"101"})
    planner = object.__new__(DeploymentReconciler)
    planner._conflict_alerted = set()
    for name, port in planner_ports(gate_stack).items():
        setattr(planner, f"_{name}", port)
    planner._managed_sweep = ManagedOfferSweep(
        session_factory=gate_stack.factory, account_id=ACCOUNT, environment="ci",
        canceller=canceller, ctx=ctx, offers=gate_stack.offers)
    assert await planner._pull_if_stopped("fUST") is True
    assert canceller.cancelled == []            # 101 failed; 555 is foreign
    canceller.fail.clear()
    assert await planner._pull_if_stopped("fUST") is True
    assert canceller.cancelled == ["101"]


@pytest.mark.asyncio
async def test_a_halt_sweep_cancels_while_market_data_was_never_seen(gate_stack):  # noqa: F811
    """The public WS down or not yet seen since boot blocks every new offer, but
    the HALTED sweep still pulls managed offers through the real command gate:
    a cancel needs no market view (chain._CANCEL_EXEMPT)."""
    from bfx_funding_bot.core.health import HealthProbe
    from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
    from bfx_funding_bot.modules.execution.managed_cancel import ManagedOfferSweep
    from bfx_funding_bot.modules.execution.safety.hard_guards import HeartbeatGuard

    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)  # managed offer 101 while the market was seen
    await gate_stack.snapshot("1000", offers=(Venue("101", "499.99990500"), Venue("555", "200")))
    rig.venue.received.clear()
    market_data = HealthProbe()  # a restarted process: ws_data never recorded
    rig.gate._safety_evaluator = stop_chain(
        rig.halt, HeartbeatGuard(probe=market_data, watched_sub_tasks=["ws_data"]))
    with pytest.raises(CommandGateBlocked, match="ws_data never recorded since boot"):
        await rig.gate.submit(await second_ready(rig), rig.ctx)
    assert rig.venue.received == []

    await rig.halt.transition("HALTED", cause="auto", actor="auto:identity_conflict", reason="x")
    planner = object.__new__(DeploymentReconciler)
    planner._conflict_alerted = set()
    for name, port in planner_ports(gate_stack).items():
        setattr(planner, f"_{name}", port)
    planner._managed_sweep = ManagedOfferSweep(
        session_factory=gate_stack.factory, account_id=ACCOUNT, environment="ci",
        canceller=rig.gate, ctx=rig.ctx, offers=gate_stack.offers)
    assert await planner._pull_if_stopped("fUST") is True
    assert rig.venue.received == ["101"]
