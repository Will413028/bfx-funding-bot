"""Pre-trade limits against real SQL (SQLite and PostgreSQL); only the venue is fake.

- offer_envelope: an applied policy without an envelope refuses; the operator
  amendment (dry run -> digest -> apply) writes schema 3 through apply_policy;
  the guard then bounds the offer inside the command gate's locked boundary,
  and its open-offer count reads the durable venue-offer projection.
- The command gate's throttle refuses before anything durable and trips
  HALTED/auto on sustained excess.
- The NAV-drop alert's 24h window survives a restart.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text

from bfx_funding_bot.modules.accounts.capital_amendment import (
    PolicyChanges,
    amend_capital_policy,
)
from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, VenueOfferStateRow
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
from bfx_funding_bot.modules.marketfeed.schemas import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.observability import alerts
from tests.modules.execution.safety.test_pre_trade import Book, book

from .test_capital_command_boundary import boundary, second_ready, stop_chain
from .test_capital_repository import capital_db as capital_db
from .test_capital_repository import capital_engine as capital_engine

BACKEND = Path(__file__).resolve().parents[2]


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


async def _intents(factory) -> int:
    async with factory() as session:
        return len((await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_INTENT"))).all())


FULL = PolicyChanges(max_offer_amount=Decimal("200"), min_period_days=2, max_period_days=2,
                     max_open_offers=2, rate_floor_ratio=Decimal("0.5"),
                     min_rate_apr=Decimal("0.01"))
MARKET = Book(book((0.0003, 2), (0.0001, 2), (0.0002, 2)))  # relative floor 0.0001


async def _amend(factory, runtime, changes: PolicyChanges, *,
                 digest: str | None = None) -> dict[str, Any]:
    async with factory() as session:
        report = await amend_capital_policy(session, repository=runtime.repository, symbol="fUST",
                                            changes=changes, apply_digest=digest)
        if report["status"] == "applied":
            await session.commit()
        else:
            await session.rollback()
    return report


@pytest.mark.asyncio
async def test_policy_without_an_envelope_refuses_and_the_amendment_bounds_each_offer(capital_db):
    factory, account = capital_db
    gate, venue, ready, ctx, runtime, halt = await boundary(factory, account)
    guard = OfferEnvelopeGuard(runtime=runtime, book=MARKET, clock=lambda: 1_000)
    gate._safety_evaluator = stop_chain(halt, account, guard)

    # Schema 1 (no envelope): refused inside the locked boundary, nothing durable.
    with pytest.raises(CommandGateBlocked, match="envelope_unset"):
        await gate.submit(ready, ctx)
    assert venue.received == [] and await _intents(factory) == 0

    # A first envelope needs every field; the dry run changes nothing; a stale
    # digest is refused.
    with pytest.raises(CapitalBlockedError, match="envelope_incomplete: max_period_days"):
        await _amend(factory, runtime, PolicyChanges(min_period_days=2))
    report = await _amend(factory, runtime, FULL)
    assert report["status"] == "dry_run" and report["expected_revision"] == 1
    assert report["new_schema_version"] == 3
    assert report["new_policy"]["envelope"]["min_rate_apr"] == "0.01"
    with pytest.raises(CapitalBlockedError, match="amendment_changed"):
        await _amend(factory, runtime, FULL, digest="0" * 64)
    applied = await _amend(factory, runtime, FULL, digest=report["amendment_digest"])
    assert (applied["status"], applied["new_revision"]) == ("applied", 2)
    assert (await _amend(factory, runtime, FULL))["status"] == "unchanged"

    view = await runtime.read(symbol="fUST", cell_id="a30")
    with pytest.raises(CommandGateBlocked, match=r"offer_amount 499\.999905 > max_offer_amount 200"):
        await gate.submit(replace(ready, capital_view=view), ctx)
    assert venue.received == [] and await _intents(factory) == 0
    within = replace(await second_ready(factory, account, ready), capital_view=view)
    await gate.submit(within, ctx)                          # 200 <= 200, rate on the floor
    assert len(venue.received) == 1 and await _intents(factory) == 1

    # A later amendment may change one field, and a bad value is refused.
    one = await _amend(factory, runtime, PolicyChanges(max_open_offers=1))
    assert one["new_policy"]["envelope"]["max_open_offers"] == 1
    with pytest.raises(CapitalBlockedError, match="invalid_policy"):
        await _amend(factory, runtime, PolicyChanges(max_period_days=121))


@pytest.mark.asyncio
async def test_disabling_through_the_amendment_zeroes_the_budget(capital_db):
    factory, account = capital_db
    _gate, _venue, _ready, _ctx, runtime, _halt = await boundary(factory, account)
    report = await _amend(factory, runtime, PolicyChanges(enabled=False))
    await _amend(factory, runtime, PolicyChanges(enabled=False),
                 digest=report["amendment_digest"])
    view = await runtime.read(symbol="fUST", cell_id="a30")
    assert (view.budget.max_new_offer, view.budget.reason) == (Decimal("0"), "policy_disabled")


@pytest.mark.asyncio
async def test_open_offers_are_counted_from_the_durable_projection(capital_db):
    factory, account = capital_db
    _gate, _venue, _ready, ctx, runtime, _halt = await boundary(factory, account)
    report = await _amend(factory, runtime, FULL)
    await _amend(factory, runtime, FULL, digest=report["amendment_digest"])
    guard = OfferEnvelopeGuard(runtime=runtime, book=MARKET, clock=lambda: 1_000)
    decision = DecisionPayload(decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
                               offer_rate=0.0002, offer_amount_usdt=200, offer_duration_days=2,
                               symbol="fUST")
    ctx = AccountContext(str(account), Credentials("k", "s"), Decimal("0"))
    environment = runtime.repository.environment

    async def add(offer_id: str, *, symbol: str = "fUST", terminal: bool = False) -> None:
        async with factory.begin() as session:
            session.add(VenueOfferStateRow(
                exchange_account_id=account, deployment_environment=environment,
                venue_offer_id=offer_id, symbol=symbol, amount_original=Decimal("200"),
                amount_remaining=Decimal("200"), rate=Decimal("0.0002"), period_days=2,
                status="ACTIVE", flags={}, mts_created=1, mts_updated=1, first_seen_event_seq=1,
                last_seen_event_seq=1, is_terminal=terminal))

    await add("1")
    await add("2", terminal=True)                           # executed/cancelled: not open
    await add("3", symbol="fUSD")                           # another symbol
    assert (await guard.evaluate(decision, ctx)).allowed
    await add("4")
    result = await guard.evaluate(decision, ctx)
    assert (result.allowed, result.reason) == (False, "open_offers 2 >= limit 2")


@pytest.mark.asyncio
async def test_throttle_refuses_before_anything_durable_and_trips_on_sustained_excess(
    capital_db, alert_sink,
):
    factory, account = capital_db
    gate, venue, ready, ctx, _runtime, _halt = await boundary(factory, account)
    trips = Trips()
    gate.throttle = CommandThrottle(capacity=2, refill_per_second=0.0001, trip_blocks=2,
                                    trip_window_s=300, protection=trips)
    await gate.submit(ready, ctx)                                          # token 1
    await gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                      account_id=ctx.account_id, ctx=ctx)                  # token 2
    received = list(venue.received)
    async with factory() as session:
        durable = len((await session.scalars(select(EventLogRow))).all())

    later = await second_ready(factory, account, ready)
    with pytest.raises(CommandGateBlocked, match="command_rate_limited"):
        await gate.submit(later, ctx)
    with pytest.raises(CommandGateBlocked, match="command_rate_limited"):
        await gate.cancel(venue_offer_id="101", signal_correlation_id=uuid4(),
                          account_id=ctx.account_id, ctx=ctx)
    assert venue.received == received
    async with factory() as session:
        assert len((await session.scalars(select(EventLogRow))).all()) == durable
    assert [trigger for trigger, _ in trips.calls] == [COMMAND_RATE_EXCEEDED]
    assert alert_sink.counts.get("log_only", 0) >= 2


def _reconciled(account, nav: str, at_ms: int) -> PositionReconciled:
    return PositionReconciled(symbol="fUST", account_id=str(account), n_offers=0, n_credits=0,
                              occurred_at_ms=at_ms, reserved=Decimal("0"), realized=Decimal("0"),
                              available=Decimal(nav))


@pytest.mark.asyncio
async def test_loss_window_survives_a_restart_and_the_nav_drop_still_alerts(capital_db):
    factory, account = capital_db
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
async def test_window_store_failure_never_breaks_the_reconcile_path(capital_db, monkeypatch):
    from bfx_funding_bot.modules.execution.safety import nav_pnl_source

    _factory, account = capital_db
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
def test_migration_grants_the_runtime_role_only_read_insert_and_prune(pg_container):
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
            conn.exec_driver_sql("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE "
                                 "rolname='bfx_bot') THEN CREATE ROLE bfx_bot; END IF; END $$")
            conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_bot")
        for args in (["upgrade", "head"], ["check"]):
            result = subprocess.run(["uv", "run", "alembic", *args], cwd=BACKEND,
                                    env=dict(os.environ, DATABASE_URL=url),
                                    capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
        with engine.connect() as conn:
            for privilege, expected in (("SELECT", True), ("INSERT", True), ("DELETE", True),
                                        ("UPDATE", False), ("TRUNCATE", False)):
                assert conn.scalar(text("SELECT has_table_privilege('bfx_bot', "
                                        "'nav_window_samples', :p)"), {"p": privilege}) is expected
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM bfx_bot")
        engine.dispose()
