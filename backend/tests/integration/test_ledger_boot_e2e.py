"""A ledger-authority bot process, built by ``build_daemon`` and booted on migrated PostgreSQL.

The epoch read is monkeypatched to ``ledger`` (production still refuses it, see
``test_daemon_authority_wiring``) and the venue is a fake ``VenueObservation`` fed to the real
ledger cycle, wrapped in the real effects. Everything else is the production composition.

Mutations (apply one at a time, run this file, revert):

* the boot sink's grace is 120 000 (``bot_ports.BOOT_GRACE_MS``):
  ``test_boot_closes_a_young_dangling_attempt``.
* the boot continues on ``query_admission_refused``: ``test_admission_refusal_refuses_the_boot``.
* the boot raises on ``fenced`` / ``incomplete_or_unequal``, or does not seed the streak:
  ``test_a_fenced_or_incomplete_boot_boots_with_trading_blocked``.
* the gate publishes or persists anything legacy under the ledger:
  ``test_an_accepted_boot_lets_a_submit_through_with_a_journal_row_and_a_notice``.
* the policy worker under the ledger reaches ``writer.prepare_locked`` / the event log:
  ``test_the_policy_worker_amends_the_policy_without_the_event_stream``.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.apps import bot, bot_ports
from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS
from bfx_funding_bot.core.errors import BootInvariantError
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_policy_control import CAPITAL_POLICY_REQUEST_APPLIED
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.command_boundary import CommandOutcomeNotice
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy, GuardResult, ReadyToSubmit
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitAcknowledged
from bfx_funding_bot.modules.ledger import (
    RUNTIME_GRACE_MS,
    Attempt,
    CapitalAvailable,
    CapitalBlocked,
    Scope,
)
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisRow,
    LedgerObservationRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_capital_authority,
    build_ledger_journal,
    build_observation_sink,
    build_policy_store,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.trading import CapitalPolicy
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    configure_account_env,
    seed_exchange_account,
)
from tests.modules.marketfeed.test_daemon_wiring import _write_cells_yaml

from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .test_ledger_unknown_resolver_pg import Clock, FakeVenue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCOPE = Scope(TEST_EXCHANGE_ACCOUNT_ID, "ci")
CELL = "fUST_a30"
AMOUNT = Decimal("200.00000500")
POLICY = CapitalPolicy(enabled=True, reserve_amount=Decimal("100"), max_cell_fraction=Decimal(1))


def _now() -> int:
    return time.time_ns() // 1_000_000


class _Allow:
    async def evaluate(self, decision, context) -> GuardResult:
        return GuardResult(allowed=True, guard_name="test")


class _Venue:
    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, ready, ctx, *, cid, reservation_ref):
        self.calls += 1
        return SubmittedOrder(cid=cid, venue_offer_id="m-1", outcome=SubmitAcknowledged("m-1"),
                              reservation_ref=reservation_ref)


class Env:
    def __init__(self, factory, cells_path: Path) -> None:
        self.factory = factory
        self.cells_path = cells_path
        self.venue = FakeVenue(Clock())
        self.daemons: list = []

    async def build(self):
        daemon = await bot.build_daemon(cells_yaml_path=self.cells_path, skip_ws=True)
        self.daemons.append(daemon)
        return daemon

    async def first_basis(self) -> None:
        """An accepted observation, as a previous process left it."""
        result = await build_observation_sink(
            self.factory, FakeVenue(Clock()), now_ms=_now, grace_ms=RUNTIME_GRACE_MS).run(SCOPE)
        assert result.decision == "accepted"

    async def count(self, table) -> int:
        async with self.factory() as session:
            return int(await session.scalar(select(func.count()).select_from(table)) or 0)


@pytest_asyncio.fixture
async def env(ledger_db, monkeypatch, httpx_mock, tmp_path):  # noqa: F811
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
    store = build_policy_store(SCOPE)
    async with factory.begin() as session:
        await store.apply_policy(session, symbol="fUST", policy=POLICY, expected_revision=0,
                                 source={"fixture": True})
        await store.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                 expected_revision=0, source={"fixture": True})
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
                            method="GET", json=[], is_reusable=True, is_optional=True)

    async def ledger_epoch(_session: object, *, supported: object) -> str:
        return "ledger"

    monkeypatch.setattr(bot, "read_authority", ledger_epoch)
    holder = Env(factory, _write_cells_yaml(tmp_path))
    monkeypatch.setattr(bot_ports, "BitfinexVenueObservation", lambda **_: holder.venue)
    try:
        yield holder
    finally:
        for daemon in holder.daemons:
            if daemon.writer_lock is not None:
                await daemon.writer_lock.release()
            await daemon.bitfinex_http.aclose()
            await daemon.db_engine.dispose()
        await engine.dispose()


async def _dangling_attempt(env: Env, *, started_at_ms: int) -> UUID:
    async with env.factory() as session:
        applied = await build_policy_store(SCOPE).read_applied(session, symbol="fUST")
        basis_id = await session.scalar(select(AcceptedCapitalBasisRow.id).order_by(
            AcceptedCapitalBasisRow.accepted_at_ms.desc()).limit(1))
    decision_id, attempt_id = str(uuid4()), uuid4()
    from bfx_funding_bot.modules.ledger._internal.journal import record_attempt
    async with env.factory.begin() as session:
        session.add(ExecutionDecisionRow(
            decision_id=decision_id, account_id=str(TEST_EXCHANGE_ACCOUNT_ID),
            exchange_account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci",
            reconcile_id="e2e", cell_id=CELL, symbol="fUST", signal_correlation_id=str(uuid4()),
            outcome="submitted", signal_rate=Decimal("0.0001"), applied_rate=Decimal("0.0001"),
            amount_usdt=Decimal("50"), duration_days=2, model_evidence={}, safety_result={},
            execution_policy="e2e", service_version="test", config_hash="test",
            occurred_at_ms=started_at_ms, recorded_at_ms=started_at_ms,
        ))
        await session.flush()
        await record_attempt(session, SCOPE, Attempt(
            attempt_id, decision_id, "fUST", CELL,
            {"type": "LIMIT", "symbol": "fUST", "amount": "50.000001", "rate": "0.0001",
             "period": 2, "flags": 0},
            basis_id, applied.revision_id, {}, started_at_ms,
        ))
    return attempt_id


async def _outcome_reason(env: Env, attempt_id: UUID) -> tuple[str, str | None] | None:
    async with env.factory() as session:
        row = await session.scalar(select(TransportOutcomeJournalRow).where(
            TransportOutcomeJournalRow.attempt_id == attempt_id))
    return None if row is None else (row.kind, row.reason)


async def _ready(env: Env) -> ReadyToSubmit:
    from tests.external.bitfinex.test_funding_rules import evidence
    from tests.modules.execution.deployment.test_reconciler import _valid_snapshot

    # The fake venue stamps its reads up to 400 ms after the query began; a capital read
    # refuses an observation that finished "in the future".
    await asyncio.sleep(0.6)
    view = await build_capital_authority(
        env.factory, max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS,
    ).read(_scope(), now_ms=_now())
    assert isinstance(view, CapitalAvailable), view
    decision_id, correlation = str(uuid4()), uuid4()
    async with env.factory.begin() as session:
        session.add(ExecutionDecisionRow(
            decision_id=decision_id, account_id=str(TEST_EXCHANGE_ACCOUNT_ID),
            exchange_account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci",
            reconcile_id="gate", cell_id=CELL, symbol="fUST",
            signal_correlation_id=str(correlation), outcome="ready",
            signal_rate=Decimal("0.0001"), applied_rate=Decimal("0.0001"),
            amount_usdt=AMOUNT, duration_days=2, model_evidence={}, safety_result={},
            execution_policy="gate", service_version="test", config_hash="test",
            occurred_at_ms=_now(), recorded_at_ms=_now(),
        ))
    from dataclasses import replace
    return ReadyToSubmit(
        decision=DecisionPayload(
            decision_outcome=DecisionOutcome.POST, signal_correlation_id=correlation,
            offer_rate=Decimal("0.0001"), offer_amount_usdt=AMOUNT, offer_duration_days=2,
            symbol="fUST"),
        decision_id=decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="book", model_version=None, evidence={},
        safety=GuardResult(True, "test"), capital_view=view,
        market_snapshot=replace(_valid_snapshot(), snapshot_id="book", captured_at_ms=_now(),
                                received_at_ms=_now(), max_age_ms=30_000),
        funding_amount_evidence=evidence(now=_now()),
    )


def _scope():
    from bfx_funding_bot.modules.trading import CapitalScope
    return CapitalScope(TEST_EXCHANGE_ACCOUNT_ID, "ci", "fUST", CELL)


def _ctx() -> AccountContext:
    return AccountContext(str(TEST_EXCHANGE_ACCOUNT_ID), Credentials("mock", "mock"), Decimal("0"))


async def test_an_accepted_boot_lets_a_submit_through_with_a_journal_row_and_a_notice(env) -> None:
    daemon = await env.build()
    await daemon._run_boot_recovery()

    assert await env.count(LedgerObservationRow) == 1
    assert daemon.periodic_reconcile._non_accepted == 0
    ready = await _ready(env)
    venue = _Venue()
    gate = daemon.command_gate
    gate._inner, gate._safety_evaluator = venue, _Allow()
    notices: list[CommandOutcomeNotice] = []

    async def on_notice(event: CommandOutcomeNotice) -> None:
        notices.append(event)

    daemon.bus.subscribe(CommandOutcomeNotice, on_notice)
    await gate.submit(ready, _ctx())

    assert venue.calls == 1
    assert await env.count(SubmissionAttemptJournalRow) == 1
    assert await env.count(TransportOutcomeJournalRow) == 1
    (notice,) = notices
    assert (notice.scope, notice.kind, notice.symbol, notice.venue_offer_id) == (
        SCOPE, "ack", "fUST", "m-1")
    assert await env.count(EventLogRow) == 0  # nothing of the event log, from boot to submit


async def test_boot_closes_a_young_dangling_attempt(env) -> None:
    await env.first_basis()
    attempt_id = await _dangling_attempt(env, started_at_ms=_now() - 5_000)
    daemon = await env.build()

    # Grace 0 under the writer lock: the attempt is closed UNKNOWN, so the query is admitted.
    await daemon._run_boot_recovery()

    assert await _outcome_reason(env, attempt_id) == ("unknown", "unresolved_at_boot")
    assert await env.count(EventLogRow) == 0


async def test_admission_refusal_refuses_the_boot(env) -> None:
    await env.first_basis()
    attempt_id = await _dangling_attempt(env, started_at_ms=_now() + 3_600_000)
    daemon = await env.build()

    with pytest.raises(BootInvariantError, match="query admission"):
        await daemon._run_boot_recovery()

    assert await _outcome_reason(env, attempt_id) is None


@pytest.mark.parametrize("decision", ["fenced", "incomplete_or_unequal"])
async def test_a_fenced_or_incomplete_boot_boots_with_trading_blocked(env, decision) -> None:
    await env.first_basis()
    ready = await _ready(env)  # read while the last process's basis was still good
    if decision == "fenced":
        async def move_the_clock() -> None:
            async with env.factory.begin() as session:
                await build_ledger_journal().bump_clock(session, SCOPE)

        env.venue = FakeVenue(Clock(), on_observe=move_the_clock)
    else:
        env.venue = FakeVenue(Clock(), history_complete=False)
    daemon = await env.build()

    await daemon._run_boot_recovery()

    assert daemon.periodic_reconcile._non_accepted == 1  # the streak is seeded
    async with env.factory() as session:  # no new accepted observation: only the earlier process's
        accepted = await session.scalar(select(func.count()).select_from(LedgerObservationRow)
                                        .where(LedgerObservationRow.accepted.is_(True)))
    assert accepted == 1
    read = await build_capital_authority(
        env.factory, max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS,
    ).read(_scope(), now_ms=_now())
    assert isinstance(read, CapitalBlocked) and read.reason == "snapshot_query_pending"
    venue = _Venue()
    gate = daemon.command_gate
    gate._inner, gate._safety_evaluator = venue, _Allow()
    with pytest.raises(CommandGateBlocked):
        await gate.submit(ready, _ctx())
    assert venue.calls == 0
    assert await env.count(EventLogRow) == 0


async def test_the_policy_worker_amends_the_policy_without_the_event_stream(
    env, monkeypatch,
) -> None:
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("the ledger authority must not replay the event stream")

    monkeypatch.setattr(AccountEventWriter, "prepare_locked", forbidden)
    daemon = await env.build()
    worker = daemon.capital_policy_control
    request_id = uuid4()
    async with env.factory.begin() as session:
        session.add(CapitalPolicyRequestRow(
            request_id=request_id, exchange_account_id=TEST_EXCHANGE_ACCOUNT_ID,
            deployment_environment="ci", symbol="fUST", action="disable", reason="maintenance",
            requested_by="operator", created_at_ms=_now()))

    async def allow(session, *, account_id, user) -> bool:
        return True

    worker.authority = allow
    assert await worker.tick() is True

    async with env.factory() as session:
        row = await session.get(CapitalPolicyRequestRow, request_id)
        applied = await build_policy_store(SCOPE).read_applied(session, symbol="fUST")
    assert row.state == "applied" and row.policy_revision_id == applied.revision_id
    assert applied.revision == 2 and applied.policy.enabled is False
    assert await env.count(EventLogRow) == 0
    assert CAPITAL_POLICY_REQUEST_APPLIED  # the alert name the worker emits
