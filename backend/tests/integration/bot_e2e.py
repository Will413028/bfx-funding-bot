"""One bot process per authority, built by ``bot.build_daemon`` on migrated PostgreSQL.

Shared by the composed end-to-end tests. The venue is faked at HTTP level (one state feeds
the legacy ``BootRecovery`` and the ledger ``BitfinexVenueObservation`` through the real
``BitfinexAuthREST``), and one fake clock drives the whole composition: ``bot.now_ms_utc`` is
the composition clock, every time source below ``select_bot_ports`` and the gate follows it.
The ledger authority is selected by monkeypatching ``bot.read_authority`` (production still
refuses it, see ``test_daemon_authority_wiring``).

Only an operator's authority check is replaced (the worker's ``authority``), as in
``test_ledger_boot_e2e``; everything else is the production composition.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.apps import bot
from bfx_funding_bot.apps.bot_ports import select_policy_ports
from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS
from bfx_funding_bot.apps.read_models import select_read_models
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcomeUnknown,
)
from bfx_funding_bot.modules.ledger import (
    CapitalAvailable,
    CapitalBlocked,
    ResolutionSubject,
    Scope,
    UncertaintyView,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.trading import CapitalPolicy, CapitalScope
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    configure_account_env,
    seed_exchange_account,
)
from tests.modules.marketfeed.test_daemon_wiring import _write_cells_yaml

from .test_ledger_schema_roles import ledger_db

SCOPE = Scope(TEST_EXCHANGE_ACCOUNT_ID, "ci")
CELL = "fUST_a30"
POLICY = CapitalPolicy(enabled=True, reserve_amount=Decimal("100"), max_cell_fraction=Decimal(1))
T0 = 1_700_000_000_000  # far from the wall clock: any time source off the composition shows
HTTP_STEP_MS = 100  # local time passing on each venue request


class FakeClock:
    """The composition clock: a value the test sets, advanced by every venue request."""

    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class HttpVenue:
    """Bitfinex authenticated REST, answered from one account state.

    Raw Bitfinex rows, as ``test_venue_observation.Venue`` serves them. An unknown path is a
    loud failure (recorded in ``unexpected``, answered 404).
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.wallets: list[list[Any]] = [["funding", "UST", "1000", 0, "1000"]]
        self.offers: list[list[Any]] = []
        self.credits: list[list[Any]] = []
        self.loans: list[list[Any]] = []
        self.history_offers: list[list[Any]] = []
        # A history pager that fails certifies nothing: incomplete coverage on both stacks.
        self.history_fails = False
        self.paths: list[str] = []
        self.unexpected: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.clock.now += HTTP_STEP_MS
        path = request.url.path.removeprefix("/v2/auth/r/")
        self.paths.append(path)
        if path.endswith("/hist"):
            kind = path.split("/")[1]
            if kind == "offers":
                if self.history_fails:
                    return httpx.Response(400, json=["error", 10020, "history unavailable"])
                return httpx.Response(200, json=self.history_offers)
            if kind in ("credits", "loans", "trades"):
                return httpx.Response(200, json=[])
        active = path.split("/")
        if active[0] == "wallets":
            return httpx.Response(200, json=self.wallets)
        if active[:2] == ["funding", "offers"] and "hist" not in active:
            return httpx.Response(200, json=self.offers)
        if active[:2] == ["funding", "credits"]:
            return httpx.Response(200, json=self.credits)
        if active[:2] == ["funding", "loans"]:
            return httpx.Response(200, json=self.loans)
        self.unexpected.append(path)
        body = json.loads(request.content or b"{}")
        return httpx.Response(404, json={"unexpected": path, "body": body})


class AllowGuard:
    async def evaluate(self, decision: Any, context: Any) -> GuardResult:
        return GuardResult(allowed=True, guard_name="test")


class AllowOperator:
    """The web user store behind ``operator_authorized`` is not part of this composition."""

    async def __call__(self, session: Any, *, account_id: Any, user: Any) -> bool:
        return True


class GateVenue:
    """The gate's inner executor: counts the submits that reach the venue."""

    def __init__(self, outcome: Callable[[str | None], Any]) -> None:
        self.calls = 0
        self.outcome = outcome

    async def submit(self, ready: Any, ctx: Any, *, cid: Any, reservation_ref: Any) -> SubmittedOrder:
        self.calls += 1
        return SubmittedOrder(cid=cid, venue_offer_id=None, outcome=self.outcome(None),
                              reservation_ref=reservation_ref)


def lost_response() -> GateVenue:
    """The submit POST answered 5xx: the venue may or may not have the offer."""
    return GateVenue(lambda _: SubmitOutcomeUnknown(reason="http_5xx", transport_started=True))


class ProcessDied(BaseException):
    """The bot process dies with a submit in flight (no outcome is ever journaled)."""


class DyingVenue(GateVenue):
    async def submit(self, ready: Any, ctx: Any, *, cid: Any, reservation_ref: Any) -> SubmittedOrder:
        self.calls += 1
        raise ProcessDied


def accepting() -> GateVenue:
    return GateVenue(lambda _: SubmitAcknowledged("m-1"))


class DeployRecorder:
    def __init__(self) -> None:
        self.venue_offers: list[Any] = []

    async def deploy(self, *, venue_offers: Any) -> None:
        self.venue_offers.append(venue_offers)


class BotEnv:
    def __init__(self, authority: str, factory: async_sessionmaker[AsyncSession],
                 cells_path: Path, clock: FakeClock, venue: HttpVenue,
                 alerts_sent: list[str]) -> None:
        self.authority = authority
        self.factory = factory
        self.cells_path = cells_path
        self.clock = clock
        self.venue = venue
        self.alerts = alerts_sent
        self.daemons: list[Any] = []
        self.reads = select_read_models(authority)  # type: ignore[arg-type]

    async def build(self) -> Any:
        """A bot process: composed, not yet booted. Its deployment is a recorder."""
        daemon = await bot.build_daemon(cells_yaml_path=self.cells_path, skip_ws=True)
        self.daemons.append(daemon)
        daemon.periodic_reconcile._deployment = DeployRecorder()
        return daemon

    async def restart(self, daemon: Any) -> Any:
        """The old process exits (its writer lock is released); a new one is composed."""
        await daemon.writer_lock.release()
        daemon.writer_lock = None
        return await self.build()

    async def boot(self, daemon: Any, at: int) -> None:
        self.clock.now = at
        await daemon._run_boot_recovery()

    async def tick(self, daemon: Any, at: int) -> None:
        self.clock.now = at
        await daemon.periodic_reconcile._tick()

    def deployments(self, daemon: Any) -> list[Any]:
        return daemon.periodic_reconcile._deployment.venue_offers

    def capital_scope(self, symbol: str = "fUST") -> CapitalScope:
        return CapitalScope(TEST_EXCHANGE_ACCOUNT_ID, "ci", symbol, CELL)

    async def capital(self, daemon: Any) -> CapitalAvailable | CapitalBlocked:
        authority = daemon.trading_status._exposure.authority  # the bot's own capital authority
        return await authority.read(self.capital_scope(), now_ms=self.clock())

    async def status(self, daemon: Any, symbol: str) -> dict[str, Any]:
        """The operator's trading status of one symbol (neutral across authorities)."""
        return (await daemon.trading_status.snapshot())["symbols"][symbol]

    async def uncertainties(self, state: str) -> tuple[UncertaintyView, ...]:
        async with self.factory() as session:
            return await self.reads.operator_reads.list_uncertainties(
                session, SCOPE, state=state, limit=50)  # type: ignore[arg-type]

    async def has_open(self, daemon: Any, symbol: str = "fUST") -> bool:
        return await daemon.command_gate._uncertainty_reader.has_open(None, SCOPE, symbol)

    async def resolution_records(self) -> int:
        """Stored resolutions: the legacy event log's marks, or the ledger journal's rows."""
        from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
        from bfx_funding_bot.modules.ledger.tables import ExecutionResolutionJournalRow

        if self.authority == "ledger":
            return await self.count(ExecutionResolutionJournalRow)
        async with self.factory() as session:
            return int(await session.scalar(
                select(func.count()).select_from(EventLogRow).where(
                    EventLogRow.event_type == "UNCERTAINTY_MARKED_NOT_ACCEPTED")) or 0)

    async def count(self, table: Any) -> int:
        async with self.factory() as session:
            return int(await session.scalar(select(func.count()).select_from(table)) or 0)

    async def ready(self, daemon: Any, amount: Decimal) -> ReadyToSubmit:
        """A submit the gate would take now: the capital view of this moment, its audit row."""
        from tests.external.bitfinex.test_funding_rules import evidence
        from tests.modules.execution.deployment.test_reconciler import _valid_snapshot

        now = self.clock()
        view = await self.capital(daemon)
        assert isinstance(view, CapitalAvailable), view
        decision_id, correlation = str(uuid4()), uuid4()
        async with self.factory.begin() as session:
            session.add(ExecutionDecisionRow(
                decision_id=decision_id, account_id=str(TEST_EXCHANGE_ACCOUNT_ID),
                exchange_account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci",
                reconcile_id="gate", cell_id=CELL, symbol="fUST",
                signal_correlation_id=str(correlation), outcome="ready",
                signal_rate=Decimal("0.0001"), applied_rate=Decimal("0.0001"),
                amount_usdt=amount, duration_days=2, model_evidence={}, safety_result={},
                execution_policy="gate", service_version="test", config_hash="test",
                occurred_at_ms=now, recorded_at_ms=now,
            ))
        return ReadyToSubmit(
            decision=DecisionPayload(
                decision_outcome=DecisionOutcome.POST, signal_correlation_id=correlation,
                offer_rate=Decimal("0.0001"), offer_amount_usdt=amount, offer_duration_days=2,
                symbol="fUST"),
            decision_id=decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
            market_snapshot_id="book", model_version=None, evidence={},
            safety=GuardResult(True, "test"), capital_view=view,
            market_snapshot=replace(_valid_snapshot(), snapshot_id="book", captured_at_ms=now,
                                    received_at_ms=now, max_age_ms=30_000),
            funding_amount_evidence=evidence(now=now),
        )

    def ctx(self) -> AccountContext:
        return AccountContext(str(TEST_EXCHANGE_ACCOUNT_ID), Credentials("mock", "mock"),
                              Decimal("0"))

    async def submit(self, daemon: Any, ready: ReadyToSubmit, venue: GateVenue) -> Any:
        gate = daemon.command_gate
        gate._inner, gate._safety_evaluator = venue, AllowGuard()
        return await gate.submit(ready, self.ctx())

    async def evidence_ref(self, uncertainty: UncertaintyView) -> str:
        """What the operator console cites for this subject now (the web API's own read)."""
        subject = ResolutionSubject(uncertainty.uncertainty_id, uncertainty.symbol,
                                    uncertainty.attempt_id)
        async with self.factory() as session:
            context = await self.reads.operator_evidence.resolution_context(session, SCOPE, subject)
        assert context.evidence_ref is not None, context
        return context.evidence_ref


@pytest_asyncio.fixture
async def bot_env(authority, ledger_db, monkeypatch, httpx_mock, tmp_path):
    url = ledger_db.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    configure_account_env(monkeypatch)
    for name in list(os.environ):
        if name.startswith("BFX_CANARY_") or name in (
            "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
        ):
            monkeypatch.delenv(name)
    for name, value in {
        "BFX_PHASE": "live", "BFX_DEPLOYMENT_ENV": "ci",
        "BFX_WS_CLIENT_ENABLED": "true", "BFX_FILL_TRACKER_ENABLED": "true",
        "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
        "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
        "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0", "DATABASE_URL": url,
        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[2] / "configs/safety.live.yaml"),
    }.items():
        monkeypatch.setenv(name, value)
    await seed_exchange_account(engine, capital_policies=False)
    policy = select_policy_ports(authority, SCOPE, max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS)
    async with factory.begin() as session:
        await policy.store.apply_policy(session, symbol="fUST", policy=POLICY,
                                        expected_revision=0, source={"fixture": True})
        await policy.store.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                        expected_revision=0, source={"fixture": True})
    clock = FakeClock()
    venue = HttpVenue(clock)
    httpx_mock.add_callback(venue.handler, url=re.compile(r"https://api\.bitfinex\.com/v2/auth/r/.*"),
                            method="POST", is_reusable=True, is_optional=True)
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
                            method="GET", json=[], is_reusable=True, is_optional=True)
    sent: list[str] = []
    from bfx_funding_bot.modules.observability import alerts
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: sent.append(event))
    monkeypatch.setattr(bot, "now_ms_utc", clock)
    if authority == "ledger":
        async def ledger_epoch(_session: object, *, supported: object) -> str:
            return "ledger"

        monkeypatch.setattr(bot, "read_authority", ledger_epoch)
    holder = BotEnv(authority, factory, _write_cells_yaml(tmp_path), clock, venue, sent)
    try:
        yield holder
    finally:
        for daemon in holder.daemons:
            if daemon.writer_lock is not None:
                await daemon.writer_lock.release()
            await daemon.bitfinex_http.aclose()
            await daemon.db_engine.dispose()
        await engine.dispose()
        assert venue.unexpected == [], venue.unexpected


__all__ = [
    "CELL", "POLICY", "SCOPE", "T0", "AllowOperator", "BotEnv", "DyingVenue", "FakeClock",
    "GateVenue", "HttpVenue", "ProcessDied", "accepting", "bot_env", "ledger_db", "lost_response",
]
