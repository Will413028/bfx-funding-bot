"""ADR D4' on real PostgreSQL roles: the web API queues, the account writer appends.

Everything here runs against the migrated schema with the shipped cutover grants
(runbook 6b) plus the five grants added by hand on 2026-09-22, because that is
the production state this migration must repair. Owner-only fixtures would pass
whether or not the web API could still write the ledger.
"""
import asyncio
import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.event_store.writer import (
    AccountEventWriter,
    ProjectionWriteError,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationIntent,
    ReservationUnknown,
    UncertaintyMarkedNotAccepted,
)
from bfx_funding_bot.modules.execution.release_worker import operator_authorized
from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    ResolutionScope,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from tests.modules.api.test_uncertainties_router import (
    ACCOUNT_ID,
    SCID,
    _attempt,
    _decision,
    _snapshot,
)

pytestmark = pytest.mark.integration

_BACKEND = Path(__file__).resolve().parents[2]
_PRIOR_HEAD = "a7f3c1d9e204"
# What was granted by hand on 2026-09-22 so `mark-not-accepted` could append.
_HAND_GRANTS = """
GRANT INSERT ON public.event_log TO bfx_webapi;
GRANT USAGE ON SEQUENCE public.event_log_event_seq_seq TO bfx_webapi;
GRANT SELECT, INSERT ON public.event_prefix_hashes TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE ON public.projection_heads TO bfx_webapi;
GRANT UPDATE ON public.execution_uncertainties TO bfx_webapi;
"""
_LEDGER_AND_PROJECTIONS = (
    "event_log", "event_prefix_hashes", "projection_heads", "execution_uncertainties",
    "offer_claims", "position_state", "reconcile_observation", "submission_attempts",
    "venue_credit_state", "venue_offer_state", "capital_snapshots", "capital_policy_revisions",
    "execution_decisions", "canary_command_permits", "trading_halt",
)
_REQUEST_COLUMNS = (
    "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id", "action",
    "reconcile_event_seq", "venue_offer_id", "decision", "reason", "requested_by",
    "created_at_ms",
)
_WORKER_COLUMNS = ("state", "processed_at_ms", "resolved_event_seq", "outcome_reason")


def _alembic(url: str, *args: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", *args], cwd=_BACKEND,
        env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def migrated(pg_container):
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        for role in ("bfx_bot", "bfx_webapi"):
            conn.exec_driver_sql(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') "
                f"THEN CREATE ROLE {role}; END IF; END $$"
            )
            conn.exec_driver_sql(f"ALTER ROLE {role} NOSUPERUSER NOCREATEROLE NOINHERIT")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
        # The account writer owns ledger writes; give it the runtime's reach.
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_bot"
        )
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO bfx_bot"
        )
    _alembic(url, "upgrade", _PRIOR_HEAD)
    runbook = (_BACKEND.parent / "docs/runbooks/halt-1-exchange-account-cutover.md").read_text()
    current_6b = runbook.split("### 6b.", 1)[1].split("```sql\n", 1)[1].split("```", 1)[0]
    # Production applied 6b before the outbox existed; its D4' lines come later.
    baseline = re.sub(r"-- ADR D4'.*?\n\n", "\n", current_6b, flags=re.S)
    assert "uncertainty_resolution_requests" not in baseline
    with engine.begin() as conn:
        conn.exec_driver_sql("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM bfx_webapi")
        conn.exec_driver_sql(baseline)
        conn.exec_driver_sql(_HAND_GRANTS)
        assert conn.scalar(text(
            "SELECT has_table_privilege('bfx_webapi', 'public.event_log', 'INSERT')"
        )), "fixture must start from the hand-granted production state"
    _alembic(url, "upgrade", "head")
    _alembic(url, "upgrade", "head")  # re-running is a no-op
    _alembic(url, "check")
    with engine.begin() as conn:
        # Re-provisioning from today's runbook must not hand any write back.
        conn.exec_driver_sql(current_6b)
    try:
        yield url, engine
    finally:
        engine.dispose()


def _privilege(conn, sql: str, **params: str) -> bool:
    return bool(conn.scalar(text(sql), params))


def test_migration_takes_back_every_web_api_ledger_write(migrated) -> None:
    _url, engine = migrated
    with engine.connect() as conn:
        for table in _LEDGER_AND_PROJECTIONS:
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not _privilege(
                    conn, "SELECT has_table_privilege('bfx_webapi', :t, :p)",
                    t=f"public.{table}", p=privilege,
                ), (table, privilege)
            assert not _privilege(
                conn, "SELECT has_any_column_privilege('bfx_webapi', :t, 'INSERT,UPDATE')",
                t=f"public.{table}",
            ), table
        for table in ("projection_heads", "event_prefix_hashes"):
            assert not _privilege(
                conn, "SELECT has_table_privilege('bfx_webapi', :t, 'SELECT')", t=f"public.{table}"
            ), table
        for privilege in ("USAGE", "UPDATE", "SELECT"):
            assert not _privilege(
                conn,
                "SELECT has_sequence_privilege('bfx_webapi', 'public.event_log_event_seq_seq', :p)",
                p=privilege,
            ), privilege
        # The read baseline the uncertainty console depends on is untouched.
        for table in ("event_log", "execution_uncertainties", "submission_attempts", "position_state"):
            assert _privilege(
                conn, "SELECT has_table_privilege('bfx_webapi', :t, 'SELECT')", t=f"public.{table}"
            ), table


def test_outbox_grants_are_column_scoped_per_role(migrated) -> None:
    _url, engine = migrated
    table = "public.uncertainty_resolution_requests"
    with engine.connect() as conn:
        assert _privilege(conn, "SELECT has_table_privilege('bfx_webapi', :t, 'SELECT')", t=table)
        assert _privilege(conn, "SELECT has_table_privilege('bfx_bot', :t, 'SELECT')", t=table)
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            for role in ("bfx_webapi", "bfx_bot"):
                assert not _privilege(
                    conn, f"SELECT has_table_privilege('{role}', :t, :p)", t=table, p=privilege
                ), (role, privilege)
        for column in _REQUEST_COLUMNS:
            assert _privilege(
                conn, "SELECT has_column_privilege('bfx_webapi', :t, :c, 'INSERT')", t=table, c=column
            ), column
            for privilege in ("UPDATE",):
                assert not _privilege(
                    conn, "SELECT has_column_privilege('bfx_webapi', :t, :c, :p)",
                    t=table, c=column, p=privilege,
                ), column
            assert not _privilege(
                conn, "SELECT has_column_privilege('bfx_bot', :t, :c, 'UPDATE')", t=table, c=column
            ), column
        for column in _WORKER_COLUMNS:
            assert not _privilege(
                conn, "SELECT has_column_privilege('bfx_webapi', :t, :c, 'INSERT')", t=table, c=column
            ), column
            assert not _privilege(
                conn, "SELECT has_column_privilege('bfx_webapi', :t, :c, 'UPDATE')", t=table, c=column
            ), column
            assert _privilege(
                conn, "SELECT has_column_privilege('bfx_bot', :t, :c, 'UPDATE')", t=table, c=column
            ), column
        assert not _privilege(conn, "SELECT has_any_column_privilege('bfx_bot', :t, 'INSERT')", t=table)


_ACCOUNT_SQL = """INSERT INTO exchange_accounts(id,venue,label,lifecycle_status)
    VALUES ('00000000-0000-0000-0000-00000000d401','bitfinex','outbox','active')"""
_REQUEST_SQL = """INSERT INTO uncertainty_resolution_requests(
    request_id,exchange_account_id,deployment_environment,uncertainty_id,action,
    reconcile_event_seq,requested_by,created_at_ms)
    VALUES (:id,'00000000-0000-0000-0000-00000000d401','ci',:u,'mark_not_accepted',7,'op',1000)"""


def test_request_is_immutable_and_its_outcome_terminal(migrated) -> None:
    _url, engine = migrated
    uncertainty = uuid4()
    first, second = uuid4(), uuid4()
    with engine.begin() as conn:
        conn.exec_driver_sql(_ACCOUNT_SQL)
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.execute(text(_REQUEST_SQL), {"id": first, "u": uncertainty})
    denied = (
        ("bfx_webapi", "INSERT INTO uncertainty_resolution_requests(request_id,exchange_account_id,"
         "deployment_environment,uncertainty_id,action,reconcile_event_seq,requested_by,"
         f"created_at_ms,state) VALUES ('{uuid4()}','00000000-0000-0000-0000-00000000d401','ci',"
         f"'{uuid4()}','mark_not_accepted',7,'op',1000,'applied')"),
        ("bfx_webapi", "UPDATE uncertainty_resolution_requests SET state='rejected'"),
        ("bfx_bot", "UPDATE uncertainty_resolution_requests SET reason='rewritten'"),
        ("bfx_webapi", "DELETE FROM uncertainty_resolution_requests"),
    )
    for role, sql in denied:
        with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            conn.exec_driver_sql(sql)
    with engine.begin() as conn, pytest.raises(Exception, match="immutable uncertainty resolution request"):
        conn.exec_driver_sql("UPDATE uncertainty_resolution_requests SET reason='rewritten'")
    with engine.begin() as conn, pytest.raises(Exception, match="uq_uncertainty_resolution_requests_pending"):
        conn.execute(text(_REQUEST_SQL), {"id": second, "u": uncertainty})
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(
            "UPDATE uncertainty_resolution_requests SET state='rejected', processed_at_ms=2000, "
            "outcome_reason='stale_reconcile_fence'"
        )
    for sql in (
        "UPDATE uncertainty_resolution_requests SET state='failed', outcome_reason='again'",
        "UPDATE uncertainty_resolution_requests SET state='requested', processed_at_ms=NULL, outcome_reason=NULL",
    ):
        with engine.begin() as conn, pytest.raises(Exception, match="invalid uncertainty resolution transition"):
            conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
            conn.exec_driver_sql(sql)
    with engine.begin() as conn, pytest.raises(Exception, match="immutable uncertainty resolution history"):
        conn.exec_driver_sql("DELETE FROM uncertainty_resolution_requests")
    # Once the first has an outcome, the operator may ask again.
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.execute(text(_REQUEST_SQL), {"id": second, "u": uncertainty})


def _restricted_engine(url: str, role: str):
    engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))

    @event.listens_for(engine.sync_engine, "connect")
    def _set_role(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    return engine


def _operator_env(monkeypatch) -> None:
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "operator-1")
    monkeypatch.setenv("BFX_OPERATOR_ROLE", "admin")
    monkeypatch.delenv("BFX_PHASE", raising=False)


async def _set_two_factor(owner, enabled: bool) -> None:
    async with owner.begin() as session:
        await session.execute(
            text('UPDATE auth."user" SET "twoFactorEnabled"=:enabled WHERE id=\'operator-1\''),
            {"enabled": enabled},
        )


async def _seed_open_submit_unknown(owner) -> tuple[UUID, int]:
    async with owner.begin() as session:
        # The configured sole admin operator, TOTP enrolled: what
        # `release_operator_authorized` requires of whoever asked.
        await session.execute(text(
            'INSERT INTO auth."user" (id,name,email,"emailVerified","createdAt","updatedAt",'
            'role,banned,"twoFactorEnabled") VALUES (\'operator-1\',\'operator\','
            "'operator@test.invalid',true,now(),now(),'admin',false,true)"
        ))
        await session.execute(text(
            "INSERT INTO exchange_accounts(id,venue,label,lifecycle_status) "
            "VALUES (:id,'bitfinex','outbox','active')"
        ), {"id": ACCOUNT_ID})
        await session.execute(text(
            "INSERT INTO exchange_account_memberships(exchange_account_id,user_id,role) "
            "VALUES (:id,'operator-1','owner')"
        ), {"id": ACCOUNT_ID})
        session.add(_decision())
        await session.flush()
        store = PostgresEventStore(deployment_environment="ci")
        await store.append_snapshot(session, _snapshot(ACCOUNT_ID, finished_at=1000))
        reference = ReservationRef(execution_decision_id="decision-7", cid=7, signal_correlation_id=SCID)
        await store.append(session, ReservationIntent(
            symbol="fUST", cid=7, signal_correlation_id=SCID, account_id=str(ACCOUNT_ID),
            is_simulated=True, execution_decision_id="decision-7", reservation_ref=reference,
            submission_attempt=_attempt(), amount=Decimal("100"), occurred_at_ms=1050,
        ))
        await store.append(session, ReservationUnknown(
            symbol="fUST", cid=7, size_usdt=Decimal("100"), signal_correlation_id=SCID,
            account_id=str(ACCOUNT_ID), is_simulated=True, reason="connection_reset",
            occurred_at_ms=1100, reservation_ref=reference,
        ))
    async with owner.begin() as session:
        await PostgresEventStore(deployment_environment="ci").append_snapshot(
            session, _snapshot(ACCOUNT_ID, finished_at=2000, started_at=1990)
        )
    async with owner() as session:
        uncertainty_id = await session.scalar(select(ExecutionUncertaintyRow.uncertainty_id))
        reconcile_seq = await session.scalar(
            select(EventLogRow.event_seq)
            .where(EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED")
            .order_by(EventLogRow.event_seq.desc())
            .limit(1)
        )
    assert uncertainty_id is not None and reconcile_seq is not None
    return uncertainty_id, reconcile_seq


def test_web_api_queues_and_only_the_account_writer_appends(migrated, monkeypatch) -> None:
    url, _engine = migrated
    _operator_env(monkeypatch)

    async def scenario() -> None:
        owner_engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
        webapi_engine = _restricted_engine(url, "bfx_webapi")
        bot_engine = _restricted_engine(url, "bfx_bot")
        owner = async_sessionmaker(owner_engine, expire_on_commit=False)
        webapi = async_sessionmaker(webapi_engine, expire_on_commit=False)
        bot = async_sessionmaker(bot_engine, expire_on_commit=False)
        try:
            uncertainty_id, reconcile_seq = await _seed_open_submit_unknown(owner)

            # The synchronous path this replaced cannot run as the web API any more.
            with pytest.raises(ProjectionWriteError, match="permission denied"):
                async with webapi.begin() as session:
                    assert await session.scalar(text("SELECT current_user")) == "bfx_webapi"
                    await AccountEventWriter(
                        store=PostgresEventStore(deployment_environment="ci")
                    ).append(session, UncertaintyMarkedNotAccepted(
                        uncertainty_id=uncertainty_id, account_id=str(ACCOUNT_ID),
                        environment="ci", symbol="fUST", kind="submit_outcome_unknown",
                        reconcile_event_seq=reconcile_seq, resolved_by_operator_id="operator-1",
                        resolution_reason="direct", resolution_evidence={"candidate_count": 0},
                        candidate_count=0, occurred_at_ms=2500,
                    ))

            async def restricted_session():
                async with webapi() as session:
                    try:
                        yield session
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise

            app = FastAPI()
            app.include_router(build_uncertainties_router())
            app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
            app.dependency_overrides[get_session] = restricted_session
            base = f"/api/v1/exchange-accounts/{ACCOUNT_ID}"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = {"reconcileEventSeq": reconcile_seq, "operatorUuid": "operator-1",
                        "reason": "zero exact candidates", "evidence": {"candidateCount": 0}}
                first, repeat = await asyncio.gather(
                    client.post(f"{base}/uncertainties/{uncertainty_id}/mark-not-accepted", json=body),
                    client.post(f"{base}/uncertainties/{uncertainty_id}/mark-not-accepted", json=body),
                )
                assert first.status_code == repeat.status_code == 202, (first.text, repeat.text)
                request_id = first.json()["data"]["requestId"]
                assert repeat.json()["data"]["requestId"] == request_id
                listed = (await client.get(f"{base}/uncertainties", params={"state": "open"})).json()
                assert listed["data"][0]["resolutionRequest"]["requestId"] == request_id
                assert listed["data"][0]["resolutionRequest"]["state"] == "requested"

                async with owner() as session:
                    assert await session.scalar(
                        select(EventLogRow.event_seq).where(
                            EventLogRow.event_type == "UNCERTAINTY_MARKED_NOT_ACCEPTED"
                        )
                    ) is None

                worker = UncertaintyResolutionWorker(
                    session_factory=bot, scope=ResolutionScope(ACCOUNT_ID, "ci"),
                    authority=operator_authorized, clock=lambda: 3000,
                )
                assert await worker.tick() is True
                assert await worker.tick() is False

                outcome = (await client.get(
                    f"{base}/uncertainty-resolution-requests/{request_id}"
                )).json()["data"]
                assert outcome["state"] == "applied", outcome
                detail = (await client.get(f"{base}/uncertainties/{uncertainty_id}")).json()["data"]
                assert detail["state"] == "resolved"
                assert detail["resolvedEventSeq"] == outcome["resolvedEventSeq"]
                assert detail["resolutionRequest"]["state"] == "applied"
            async with owner() as session:
                resolutions = list(await session.scalars(
                    select(EventLogRow.event_seq).where(
                        EventLogRow.event_type == "UNCERTAINTY_MARKED_NOT_ACCEPTED"
                    )
                ))
            assert resolutions == [outcome["resolvedEventSeq"]]
        finally:
            for engine in (owner_engine, webapi_engine, bot_engine):
                await engine.dispose()

    asyncio.run(scenario())


def test_queueing_a_request_never_waits_for_or_holds_the_account_writer_lock(
    migrated, monkeypatch
) -> None:
    """The web API must not contend for the daemon's account lock: a slow or
    abusive request would otherwise stall reconcile and submit."""
    from bfx_funding_bot.core.writer_lock import (
        account_id_canonical,
        derive_transaction_lock_key,
    )

    url, _engine = migrated
    _operator_env(monkeypatch)

    async def scenario() -> None:
        owner_engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
        webapi_engine = _restricted_engine(url, "bfx_webapi")
        owner = async_sessionmaker(owner_engine, expire_on_commit=False)
        webapi = async_sessionmaker(webapi_engine, expire_on_commit=False)
        try:
            uncertainty_id, reconcile_seq = await _seed_open_submit_unknown(owner)

            async def restricted_session():
                async with webapi() as session:
                    try:
                        yield session
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        raise

            app = FastAPI()
            app.include_router(build_uncertainties_router())
            app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
            app.dependency_overrides[get_session] = restricted_session
            key = derive_transaction_lock_key(account_id_canonical(str(ACCOUNT_ID)), "ci")
            daemon = await owner_engine.connect()
            transaction = await daemon.begin()
            try:
                # The account writer is mid-transaction (reconcile, submit...).
                await daemon.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await asyncio.wait_for(client.post(
                        f"/api/v1/exchange-accounts/{ACCOUNT_ID}/uncertainties/"
                        f"{uncertainty_id}/mark-not-accepted",
                        json={"reconcileEventSeq": reconcile_seq, "operatorUuid": "operator-1",
                              "evidence": {"candidateCount": 0}},
                    ), timeout=5)
                assert response.status_code == 202, response.text
            finally:
                await transaction.rollback()
                await daemon.close()
        finally:
            for engine in (owner_engine, webapi_engine):
                await engine.dispose()

    asyncio.run(scenario())


def test_account_writer_can_ask_the_shared_operator_authority(migrated) -> None:
    """The worker calls the SECURITY DEFINER check as bfx_bot, never reading auth."""
    _url, engine = migrated
    signature = "public.release_operator_authorized(uuid,text)"
    with engine.connect() as conn:
        assert _privilege(
            conn, "SELECT has_function_privilege('bfx_bot', :f, 'EXECUTE')", f=signature
        )
        assert not _privilege(
            conn, "SELECT has_function_privilege('bfx_webapi', :f, 'EXECUTE')", f=signature
        )
        assert not _privilege(
            conn, "SELECT has_table_privilege('bfx_bot', 'auth.\"user\"', 'SELECT')"
        )


def test_revoked_operator_request_is_rejected_by_the_account_writer(
    migrated, monkeypatch
) -> None:
    url, _engine = migrated
    _operator_env(monkeypatch)

    async def scenario() -> None:
        owner_engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
        bot_engine = _restricted_engine(url, "bfx_bot")
        owner = async_sessionmaker(owner_engine, expire_on_commit=False)
        bot = async_sessionmaker(bot_engine, expire_on_commit=False)
        scope = ResolutionScope(ACCOUNT_ID, "ci")
        try:
            uncertainty_id, reconcile_seq = await _seed_open_submit_unknown(owner)

            async def queue(created_at_ms: int) -> UUID:
                request_id = uuid4()
                async with owner.begin() as session:
                    await session.execute(text(
                        "INSERT INTO uncertainty_resolution_requests(request_id,exchange_account_id,"
                        "deployment_environment,uncertainty_id,action,reconcile_event_seq,"
                        "requested_by,created_at_ms) VALUES (:id,:account,'ci',:u,"
                        "'mark_not_accepted',:seq,'operator-1',:at)"
                    ), {"id": request_id, "account": ACCOUNT_ID, "u": uncertainty_id,
                        "seq": reconcile_seq, "at": created_at_ms})
                return request_id

            async def outcome(request_id: UUID):
                async with owner() as session:
                    return (await session.execute(text(
                        "SELECT state, outcome_reason, resolved_event_seq "
                        "FROM uncertainty_resolution_requests WHERE request_id=:id"
                    ), {"id": request_id})).one()

            async def resolutions() -> int:
                async with owner() as session:
                    return int(await session.scalar(text(
                        "SELECT count(*) FROM event_log "
                        "WHERE event_type='UNCERTAINTY_MARKED_NOT_ACCEPTED'"
                    )))

            worker = UncertaintyResolutionWorker(
                session_factory=bot, scope=scope, authority=operator_authorized,
                clock=lambda: 3000,
            )
            # Accepted while enrolled, then TOTP is removed before the daemon applies.
            revoked = await queue(2500)
            await _set_two_factor(owner, False)
            assert await worker.tick() is True
            assert tuple(await outcome(revoked)) == ("rejected", "operator_revoked", None)
            assert await resolutions() == 0
            async with owner() as session:
                assert await session.scalar(
                    select(ExecutionUncertaintyRow.state).where(
                        ExecutionUncertaintyRow.uncertainty_id == uncertainty_id
                    )
                ) == "open"

            # Re-enrolled, the same operator's fresh request applies.
            await _set_two_factor(owner, True)
            restored = await queue(2600)
            assert await worker.tick() is True
            state, reason, event_seq = await outcome(restored)
            assert (state, reason) == ("applied", None) and event_seq is not None
            assert await resolutions() == 1
        finally:
            for engine in (owner_engine, bot_engine):
                await engine.dispose()

    asyncio.run(scenario())
