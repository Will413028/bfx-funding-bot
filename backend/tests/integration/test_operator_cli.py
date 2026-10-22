"""The operator CLI as ``bfx_operator``: it queues, the account daemon's workers apply.

The CLI logs in with its own role (production's host setup: no default privileges, no schema
grant before the migration), so every read and insert below is one ``4ab02765a34b`` granted.
Each request goes through the real worker of its outbox; no web API is built.
"""
from __future__ import annotations

import asyncio
import io
import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from bfx_funding_bot.apps import operator_cli
from bfx_funding_bot.modules.execution.capital_policy_control import CapitalPolicyRequestWorker
from bfx_funding_bot.modules.execution.trading_control import TradingControlWorker
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionScope,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.ledger import Quarantine, observation_evidence_ref
from bfx_funding_bot.modules.ledger.wiring import (
    build_operator_resolution,
    build_policy_store,
    build_scope_lock,
)
from tests.pg_templates import alembic, stamp_realm

from . import test_ledger_capital_reader
from .test_ledger_operator_resolution_pg import open_unknown
from .test_ledger_schema_roles import _prepare
from .test_ledger_unknown_resolver_pg import JOURNAL, SCOPE, start, venue_offer

pytestmark = pytest.mark.integration

ROLE = "bfx_operator"
PASSWORD = "operator-cli-test"
TAILNET = "4182486213003443"
OPERATOR = TAILNET  # the caller's tailnet identity is the requester (9d-6)
ACCOUNT = SCOPE.exchange_account_id
WORKER_NOW = 2_000_000

# The ledger book over this module's ``ledger_db`` (a fixture is found by its module name).
book = test_ledger_capital_reader.book


def _build(url: str) -> None:
    _prepare(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{ROLE}') "
                             f"THEN CREATE ROLE {ROLE}; END IF; END $$")
        conn.exec_driver_sql(f"ALTER ROLE {ROLE} LOGIN PASSWORD '{PASSWORD}'")
        conn.exec_driver_sql(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    engine.dispose()
    alembic(url, "upgrade", "head")
    stamp_realm(url, "ci")


@pytest.fixture
def ledger_db(pg_templates, pg_clone):
    """``book``'s database, built with the operator role (overrides the shared fixture)."""
    engine = create_engine(pg_clone(pg_templates.template("operator_cli_ledger", _build)))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def operator_env(ledger_db, monkeypatch) -> None:
    url = make_url(ledger_db.url.render_as_string(hide_password=False)).set(
        drivername="postgresql", username=ROLE, password=PASSWORD)
    for key, value in {
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "BFX_EXCHANGE_ACCOUNT_ID": str(ACCOUNT),
        # The keys operator.env keeps until W10; the CLI must ignore them.
        "BFX_OPERATOR_USER_ID": "operator-1",
        "BFX_OPERATOR_TAILNET_USER_ID": "999",
        "BFX_OPERATOR_TAILNET_IDENTITY": TAILNET,
        "BFX_DEPLOYMENT_ENV": "ci",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("BFX_PHASE", raising=False)


async def cli(*argv: str) -> tuple[int, dict[str, Any], str]:
    """``main`` in a thread: it runs its own event loop, as the container's entrypoint does."""
    out, log = io.StringIO(), io.StringIO()
    code = await asyncio.to_thread(operator_cli.main, list(argv), out=out, log=log)
    payload = json.loads(out.getvalue())
    if code != 0:
        # Shown only when a test fails: this file has failed intermittently in CI (KeyError on
        # the payload) with no trace of which refusal or exception the CLI hit.
        print(f"cli {argv[0]} exit={code} payload={payload} log={log.getvalue()!r}")
    return code, payload, log.getvalue()


async def allow(session, *, account_id, user) -> bool:
    return user == OPERATOR


async def member(book_, role: str = "owner") -> None:
    async with book_.factory.begin() as session:
        await session.execute(text(
            "INSERT INTO exchange_account_memberships(exchange_account_id,user_id,role) "
            "VALUES (:a,:u,:r)"), {"a": ACCOUNT, "u": OPERATOR, "r": role})


class KillSwitch:
    def __init__(self) -> None:
        self.requests: list[UUID | None] = []

    async def currencies(self):
        return ("UST",), None

    async def engage(self, *, cause, actor, reason, when_already_halted="retry",
                     operator_request_id=None):
        self.requests.append(operator_request_id)


@pytest.mark.asyncio
async def test_resume_and_kill(book, operator_env) -> None:
    await member(book)
    await start(book)
    worker = TradingControlWorker(session_factory=book.factory, account_id=ACCOUNT,
                                  environment="ci", authority=allow,
                                  clock=lambda: WORKER_NOW)
    worker.kill_switch = kill = KillSwitch()

    code, queued, log = await cli("resume", "--reason", "back to work", "--no-wait")
    assert (code, queued["state"], queued["requested_by"]) == (0, "requested", OPERATOR)
    assert log == (f"operator_request request_id={queued['request_id']} action=resume "
                   f"tailnet_identity={TAILNET}\n")
    # One pending request per action: a second resume is refused before the outbox.
    assert (await cli("resume", "--reason", "again", "--no-wait"))[:2] == (
        3, {"error": "request_pending", "kind": "conflict"})
    assert await worker.tick() is True
    code, applied, _ = await cli("request", "trading", queued["request_id"])
    assert (code, applied["state"], applied["outcome_reason"]) == (0, "applied", "resumed")

    # Waiting (the default) returns once the worker has applied it.
    waiting = asyncio.create_task(cli("kill", "--reason", "stop now", "--wait-seconds", "60"))
    while not waiting.done():
        await worker.tick()
        await asyncio.sleep(0.2)
    code, killed, _ = await waiting
    assert (code, killed["action"], killed["state"]) == (0, "kill", "applied")
    # The kill's cancel-all names the request (an idle tick may retry it: no audit here).
    assert set(kill.requests) == {UUID(killed["request_id"])}

    code, status, _ = await cli("status")
    assert code == 0
    assert (status["trading_state"]["state"], status["trading_state"]["actor"]) == (
        "HALTED", OPERATOR)
    assert [r["action"] for r in status["requests"]] == ["kill", "resume"]
    assert [c["symbol"] for c in status["currencies"]] == ["fUST"]


@pytest.mark.asyncio
async def test_currency_enable_and_disable(book, operator_env) -> None:
    await member(book)
    await start(book)
    worker = CapitalPolicyRequestWorker(
        session_factory=book.factory, account_id=ACCOUNT, environment="ci", authority=allow,
        policy_store=build_policy_store(SCOPE), scope_lock=build_scope_lock(),
        clock=lambda: WORKER_NOW)
    code, queued, _ = await cli("disable", "fUST", "--reason", "pause fUST", "--no-wait")
    assert (code, queued["symbol"], queued["state"]) == (0, "fUST", "requested")
    assert await worker.tick() is True
    assert (await cli("request", "currency", queued["request_id"]))[1]["state"] == "applied"
    (currency,) = (await cli("status"))[1]["currencies"]
    assert currency["enabled"] is False
    # A currency without an applied policy is refused before the outbox.
    assert (await cli("enable", "fBTC", "--reason", "x", "--no-wait"))[:2] == (
        3, {"error": "policy_unavailable", "kind": "not_found"})


async def _settle(book_) -> None:
    worker = UncertaintyResolutionWorker(
        session_factory=book_.factory, scope=ResolutionScope(ACCOUNT, "ci"), authority=allow,
        clock=lambda: WORKER_NOW, resolution=build_operator_resolution())
    assert await worker.tick() is True


@pytest.mark.parametrize("action", ["mark-not-accepted", "bind-to-venue"])
@pytest.mark.asyncio
async def test_attempt_adjudications(book, operator_env, action) -> None:
    await member(book)
    uncertainty, ref = await open_unknown(
        book, offers=(venue_offer("V1"),) if action == "bind-to-venue" else ())
    code, listed, _ = await cli("uncertainties", "--state", "open")
    assert code == 0
    (row,) = listed["uncertainties"]
    assert (row["uncertainty_id"], row["resolution_context"]["evidence_ref"]) == (
        str(uncertainty), ref)
    assert "evidence" not in row  # only the bounded summary

    extra = ["--venue-offer-id", "V1"] if action == "bind-to-venue" else []
    code, queued, _ = await cli(action, str(uncertainty), "--evidence-ref", ref,
                                "--reason", "checked at the venue", *extra, "--no-wait")
    assert (code, queued["state"], queued["evidence_ref"]) == (0, "requested", ref)
    await _settle(book)
    code, applied, _ = await cli("request", "resolution", queued["request_id"])
    assert (code, applied["state"]) == (0, "applied"), applied
    code, detail, _ = await cli("uncertainty", str(uncertainty))
    assert (detail["state"], detail["resolved_by_operator_id"]) == ("resolved", OPERATOR)
    assert detail["resolution_request"] == applied


@pytest.mark.asyncio
async def test_manual_resolution(book, operator_env) -> None:
    await member(book)
    await start(book, at=1)
    quarantine = uuid4()
    async with book.factory.begin() as session:
        await JOURNAL.open_quarantine(
            session, SCOPE, Quarantine(quarantine, "fUST", Decimal("10"), 0, {}))
    ref = observation_evidence_ref(book.observation_id)
    code, queued, _ = await cli("manual-resolution", str(quarantine), "--evidence-ref", ref,
                                "--decision", "closed_at_venue", "--reason", "checked",
                                "--no-wait")
    assert (code, queued["action"]) == (0, "manual_resolution"), queued
    await _settle(book)
    assert (await cli("request", "resolution", queued["request_id"]))[1]["state"] == "applied"


@pytest.mark.asyncio
async def test_refusals_come_before_the_outbox(book, operator_env, monkeypatch) -> None:
    await member(book, role="viewer")
    uncertainty, ref = await open_unknown(book)
    # A viewer reads, never writes.
    assert (await cli("status"))[0] == 0
    assert (await cli("mark-not-accepted", str(uncertainty), "--evidence-ref", ref,
                      "--no-wait"))[:2] == (
        3, {"error": "account_write_required", "kind": "forbidden"})
    # Not a member: refused before anything is queued, a write included.
    monkeypatch.setenv("BFX_OPERATOR_TAILNET_IDENTITY", "1")
    assert (await cli("status"))[:2] == (77, {"error": "operator_not_member"})
    assert (await cli("mark-not-accepted", str(uncertainty), "--evidence-ref", ref,
                      "--no-wait"))[:2] == (77, {"error": "operator_not_member"})
    # A malformed identity is refused before anything connects.
    monkeypatch.setenv("BFX_OPERATOR_TAILNET_IDENTITY", "operator-1")
    assert (await cli("status"))[:2] == (77, {"error": "operator_identity_invalid"})
    async with book.factory() as session:
        assert await session.scalar(text(
            "SELECT count(*) FROM uncertainty_resolution_requests")) == 0


@pytest.mark.asyncio
async def test_a_schema_head_this_image_does_not_run_on_is_refused(book, operator_env) -> None:
    await member(book)
    async with book.factory.begin() as session:
        await session.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))
    assert (await cli("status"))[:2] == (78, {"error": "schema_head_mismatch"})


@pytest.mark.asyncio
async def test_the_credential_reads_need_no_more_than_the_role_has(book, operator_env) -> None:
    """``credential`` and ``request credential`` as ``bfx_operator`` (rotation itself:
    tests/integration/test_credential_rotation.py)."""
    await member(book)
    code, view, _ = await cli("credential")
    assert (code, view) == (0, {"credential": None, "latest_request": None})
    assert (await cli("request", "credential", str(uuid4())))[:2] == (
        3, {"error": "not_found", "kind": "not_found"})
