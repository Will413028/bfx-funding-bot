"""ADR D4' on real PostgreSQL roles: the web API queues, the account writer applies.

The historical fixture below reproduces the manual baseline and the five extra
grants from 2026-09-22. Migrations must repair that state and provision today's
permissions without manual grants. Owner-only fixtures would pass whether or not
the web API could still write the ledger.
"""
import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import bfx_funding_bot.modules.execution.audit.tables  # noqa: F401
from bfx_funding_bot.apps.read_models import build_read_models
from bfx_funding_bot.core.auth import Principal, require_operator
from bfx_funding_bot.modules.api.deps import get_session
from bfx_funding_bot.modules.api.uncertainties import build_uncertainties_router
from bfx_funding_bot.modules.execution.legacy_archive import qualified
from bfx_funding_bot.modules.execution.operator_requests import operator_authorized
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionScope,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.ledger.tables import ExecutionResolutionJournalRow
from bfx_funding_bot.modules.ledger.wiring import build_operator_reads, build_operator_resolution
from tests.pg_templates import alembic as _alembic
from tests.pg_templates import stamp_realm

from .test_ledger_capital_reader import book  # noqa: F401 - fixture re-export
from .test_ledger_operator_resolution_pg import open_unknown
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .test_ledger_unknown_resolver_pg import SCOPE as LEDGER_SCOPE

pytestmark = pytest.mark.integration

_PRIOR_HEAD = "a7f3c1d9e204"
# Fixed pre-outbox baseline from the retired Halt 1 runbook, not current policy.
_LEGACY_WEBAPI_BASELINE = """
GRANT USAGE ON SCHEMA public TO bfx_webapi;
GRANT SELECT (version_num) ON TABLE public.alembic_version TO bfx_webapi;
GRANT SELECT ON TABLE
  public.user_profiles,
  public.exchange_accounts,
  public.exchange_account_memberships,
  public.position_state,
  public.offer_claims,
  public.event_log,
  public.execution_uncertainties,
  public.submission_attempts,
  public.attribution_weekly,
  public.funding_candles
TO bfx_webapi;
GRANT INSERT ON TABLE public.user_profiles TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE ON TABLE public.exchange_account_credentials TO bfx_webapi;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.account_config_drafts TO bfx_webapi;
"""
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
    "execution_decisions",
)
_REQUEST_COLUMNS = (
    "request_id", "exchange_account_id", "deployment_environment", "uncertainty_id", "action",
    "reconcile_event_seq", "observation_id", "venue_offer_id", "decision", "reason",
    "requested_by", "created_at_ms",
)
_WORKER_COLUMNS = ("state", "processed_at_ms", "resolved_event_seq", "outcome_reason")


def _build_migrated(url: str) -> None:
    """The hand-granted production state, then the upgrade that must repair it."""
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS legacy_archive CASCADE")
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
    with engine.begin() as conn:
        conn.exec_driver_sql("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM bfx_webapi")
        conn.exec_driver_sql(_LEGACY_WEBAPI_BASELINE)
        conn.exec_driver_sql(_HAND_GRANTS)
        assert conn.scalar(text(
            "SELECT has_table_privilege('bfx_webapi', 'public.event_log', 'INSERT')"
        )), "fixture must start from the hand-granted production state"
    _alembic(url, "upgrade", "head")
    _alembic(url, "upgrade", "head")  # re-running is a no-op
    _alembic(url, "check")
    stamp_realm(url, "ci")
    engine.dispose()


@pytest.fixture
def migrated(pg_templates, pg_clone):
    """A fresh copy of the repaired database; the upgrade runs once per session."""
    url = pg_clone(pg_templates.template("uncertainty_outbox_migrated", _build_migrated))
    engine = create_engine(url)
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
                    t=qualified(table), p=privilege,
                ), (table, privilege)
            assert not _privilege(
                conn, "SELECT has_any_column_privilege('bfx_webapi', :t, 'INSERT,UPDATE')",
                t=qualified(table),
            ), table
        for table in ("projection_heads", "event_prefix_hashes"):
            assert not _privilege(
                conn, "SELECT has_table_privilege('bfx_webapi', :t, 'SELECT')", t=qualified(table)
            ), table
        for privilege in ("USAGE", "UPDATE", "SELECT"):
            assert not _privilege(
                conn,
                "SELECT has_sequence_privilege('bfx_webapi', "
                "'legacy_archive.event_log_event_seq_seq', :p)",
                p=privilege,
            ), privilege
        # Since c2d3e4f5a6b7 the legacy tables are archived: the web API keeps only the
        # archived execution history's column read of event_log.
        assert _privilege(
            conn, "SELECT has_column_privilege('bfx_webapi', :t, 'payload', 'SELECT')",
            t=qualified("event_log"),
        )
        for table in ("event_log", "execution_uncertainties", "submission_attempts", "position_state"):
            assert not _privilege(
                conn, "SELECT has_table_privilege('bfx_webapi', :t, 'SELECT')", t=qualified(table)
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
_LEGACY_EPOCH_SQL = """INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason)
    SELECT max(epoch_seq) + 1, 'legacy', 0, 'test', 'pre-switch request shape'
    FROM capital_authority_epoch"""
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
        # The outbox rules hold under either epoch; a request citing an event sequence is the
        # pre-switch shape (the ledger epoch closes it: test_ledger_operator_resolution_pg).
        conn.exec_driver_sql(_LEGACY_EPOCH_SQL)
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


async def _seed_open_submit_unknown(book_) -> tuple[UUID, str]:
    """The configured sole admin operator, TOTP enrolled and a member of the account (what
    ``operator_authorized`` requires of whoever asked), and an UNKNOWN submit with the ref of
    the ledger observation an operator cites to mark it not accepted."""
    async with book_.factory.begin() as session:
        await session.execute(text(
            'INSERT INTO auth."user" (id,name,email,"emailVerified","createdAt","updatedAt",'
            'role,banned,"twoFactorEnabled") VALUES (\'operator-1\',\'operator\','
            "'operator@test.invalid',true,now(),now(),'admin',false,true)"
        ))
        await session.execute(text(
            "INSERT INTO exchange_account_memberships(exchange_account_id,user_id,role) "
            "VALUES (:id,'operator-1','owner')"
        ), {"id": LEDGER_SCOPE.exchange_account_id})
    return await open_unknown(book_)


def _url(ledger_db_) -> str:
    return ledger_db_.url.render_as_string(hide_password=False)


async def _journal_rows(owner) -> int:
    async with owner() as session:
        return int(await session.scalar(
            select(func.count()).select_from(ExecutionResolutionJournalRow)) or 0)


def test_web_api_queues_and_only_the_account_writer_applies(book, ledger_db, monkeypatch) -> None:  # noqa: F811
    _operator_env(monkeypatch)
    url = _url(ledger_db)

    async def scenario() -> None:
        uncertainty_id, ref = await _seed_open_submit_unknown(book)
        owner = book.factory
        webapi_engine = _restricted_engine(url, "bfx_webapi")
        bot_engine = _restricted_engine(url, "bfx_bot")
        webapi = async_sessionmaker(webapi_engine, expire_on_commit=False)
        bot = async_sessionmaker(bot_engine, expire_on_commit=False)
        account = LEDGER_SCOPE.exchange_account_id
        try:
            # The web API holds no write on the resolution journal: only the worker applies.
            with pytest.raises(Exception, match="permission denied"):
                async with webapi.begin() as session:
                    await session.execute(text(
                        "INSERT INTO execution_resolution_journal(id) VALUES (gen_random_uuid())"))

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
            app.state.read_models = build_read_models()
            app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
            app.dependency_overrides[get_session] = restricted_session
            base = f"/api/v1/exchange-accounts/{account}"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = {"evidenceRef": ref, "operatorUuid": "operator-1",
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
                assert await _journal_rows(owner) == 0  # queued, not applied

                worker = UncertaintyResolutionWorker(
                    session_factory=bot, scope=ResolutionScope(account, "ci"),
                    authority=operator_authorized, clock=lambda: 3000,
                    resolution=build_operator_resolution())
                assert await worker.tick() is True
                assert await worker.tick() is False

                outcome = (await client.get(
                    f"{base}/uncertainty-resolution-requests/{request_id}"
                )).json()["data"]
                assert outcome["state"] == "applied", outcome
                detail = (await client.get(f"{base}/uncertainties/{uncertainty_id}")).json()["data"]
                assert detail["state"] == "resolved"
                assert detail["resolutionRequest"] == outcome
            async with owner() as session:
                (stored,) = list(await session.scalars(select(ExecutionResolutionJournalRow)))
            assert (stored.attempt_id, stored.action, stored.operator_request_id) == (
                uncertainty_id, "not_accepted", UUID(request_id))
        finally:
            for engine in (webapi_engine, bot_engine):
                await engine.dispose()

    asyncio.run(scenario())


def test_queueing_a_request_never_waits_for_or_holds_the_account_writer_lock(
    book, ledger_db, monkeypatch  # noqa: F811
) -> None:
    """The web API must not contend for the daemon's account lock: a slow or
    abusive request would otherwise stall reconcile and submit."""
    from bfx_funding_bot.core.writer_lock import (
        account_id_canonical,
        derive_transaction_lock_key,
    )

    _operator_env(monkeypatch)
    url = _url(ledger_db)

    async def scenario() -> None:
        uncertainty_id, ref = await _seed_open_submit_unknown(book)
        account = LEDGER_SCOPE.exchange_account_id
        owner_engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
        webapi_engine = _restricted_engine(url, "bfx_webapi")
        webapi = async_sessionmaker(webapi_engine, expire_on_commit=False)
        try:
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
            app.state.read_models = build_read_models()
            app.dependency_overrides[require_operator] = lambda: Principal("operator-1", None, "admin")
            app.dependency_overrides[get_session] = restricted_session
            key = derive_transaction_lock_key(account_id_canonical(str(account)), "ci")
            daemon = await owner_engine.connect()
            transaction = await daemon.begin()
            try:
                # The account writer is mid-transaction (reconcile, submit...).
                await daemon.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await asyncio.wait_for(client.post(
                        f"/api/v1/exchange-accounts/{account}/uncertainties/"
                        f"{uncertainty_id}/mark-not-accepted",
                        json={"evidenceRef": ref, "operatorUuid": "operator-1",
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
    signature = "public.operator_authorized(uuid,text)"
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
    book, ledger_db, monkeypatch  # noqa: F811
) -> None:
    _operator_env(monkeypatch)
    url = _url(ledger_db)

    async def scenario() -> None:
        uncertainty_id, ref = await _seed_open_submit_unknown(book)
        owner = book.factory
        account = LEDGER_SCOPE.exchange_account_id
        bot_engine = _restricted_engine(url, "bfx_bot")
        bot = async_sessionmaker(bot_engine, expire_on_commit=False)
        scope = ResolutionScope(account, "ci")
        observation_id = UUID(ref.rsplit(":", 1)[1])
        try:
            async def queue(created_at_ms: int) -> UUID:
                request_id = uuid4()
                async with owner.begin() as session:
                    await session.execute(text(
                        "INSERT INTO uncertainty_resolution_requests(request_id,exchange_account_id,"
                        "deployment_environment,uncertainty_id,action,observation_id,"
                        "requested_by,created_at_ms) VALUES (:id,:account,'ci',:u,"
                        "'mark_not_accepted',:o,'operator-1',:at)"
                    ), {"id": request_id, "account": account, "u": uncertainty_id,
                        "o": observation_id, "at": created_at_ms})
                return request_id

            async def outcome(request_id: UUID):
                async with owner() as session:
                    return (await session.execute(text(
                        "SELECT state, outcome_reason FROM uncertainty_resolution_requests "
                        "WHERE request_id=:id"
                    ), {"id": request_id})).one()

            worker = UncertaintyResolutionWorker(
                session_factory=bot, scope=scope, authority=operator_authorized,
                clock=lambda: 3000, resolution=build_operator_resolution())
            # Accepted while enrolled, then TOTP is removed before the daemon applies.
            revoked = await queue(2500)
            await _set_two_factor(owner, False)
            assert await worker.tick() is True
            assert tuple(await outcome(revoked)) == ("rejected", "operator_not_authorized")
            assert await _journal_rows(owner) == 0
            async with owner() as session:
                view = await build_operator_reads().get_uncertainty(
                    session, LEDGER_SCOPE, uncertainty_id)
            assert view is not None and view.state == "open"

            # Re-enrolled, the same operator's fresh request applies.
            await _set_two_factor(owner, True)
            restored = await queue(2600)
            assert await worker.tick() is True
            assert tuple(await outcome(restored)) == ("applied", None)
            assert await _journal_rows(owner) == 1
        finally:
            await bot_engine.dispose()

    asyncio.run(scenario())
