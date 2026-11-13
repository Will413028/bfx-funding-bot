"""545b2877e9bd: live tables lose ``deployment_environment`` and the per-row realm trigger.

On a disposable PostgreSQL, from the previous revision (``ci`` stamp): the upgrade keeps every
row, rebuilds each narrowed key and foreign key under its original name, leaves the archive
rows byte for byte, and refuses (staying at the previous revision) a database holding a row of
another realm. At head the rewritten trigger functions still refuse what they refused, now on
``exchange_account_id`` alone.

Mutations (each in a copy of the migration; the named test fails):

* skip one table's ``DROP COLUMN``: ``test_head_has_no_realm_column_trigger_or_function``
  (and the migration's own end assertion);
* leave one trigger function unrewritten: the same test's ``prosrc`` check;
* do not rebuild a foreign key: ``test_the_upgrade_keeps_rows_and_rebuilds_keys_by_name``;
* change an archive row during the upgrade: ``test_archive_rows_are_unchanged``;
* drop the realm assertion: ``test_a_row_of_another_realm_refuses_the_upgrade``;
* keep the column on one live table: ``test_a_statement_of_the_previous_version_cannot_run``.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.pg_templates import alembic, template_at

pytestmark = pytest.mark.integration

_REVISION = "545b2877e9bd"
_BEFORE = "96535191e443"
_A = "00000000-0000-4000-8000-0000000000a1"
_B = "00000000-0000-4000-8000-0000000000b2"
_ARCHIVES = ("legacy_archive", "release_archive")
_REWRITTEN = (
    "projection_audit.guard_run", "public.guard_credential_rotation_request",
    "public.guard_ledger_mirror", "public.guard_ledger_observation_accept",
    "public.guard_ledger_quarantine_source", "public.guard_ledger_scope",
    "public.guard_runtime_policy_head", "public.guard_runtime_policy_revision",
    "public.guard_trading_state_transition",
)
_FOREIGN_KEYS = {
    "fk_capital_policy_revisions_operator_request":
        "FOREIGN KEY (operator_request_id, exchange_account_id, symbol) REFERENCES "
        "capital_policy_requests(request_id, exchange_account_id, symbol) ON DELETE RESTRICT",
    "fk_execution_resolution_journal_request_attempt":
        "FOREIGN KEY (operator_request_id, exchange_account_id, attempt_id) REFERENCES "
        "uncertainty_resolution_requests(request_id, exchange_account_id, uncertainty_id) "
        "ON DELETE RESTRICT",
    "fk_execution_resolution_journal_request_quarantine":
        "FOREIGN KEY (operator_request_id, exchange_account_id, quarantine_id) REFERENCES "
        "uncertainty_resolution_requests(request_id, exchange_account_id, uncertainty_id) "
        "ON DELETE RESTRICT",
    "fk_funding_cancel_all_audit_operator_request":
        "FOREIGN KEY (operator_request_id, exchange_account_id) REFERENCES "
        "trading_control_requests(request_id, exchange_account_id) ON DELETE RESTRICT",
    "fk_funding_cancel_all_audit_trading_state":
        "FOREIGN KEY (trading_state_id, exchange_account_id) REFERENCES "
        "trading_state(id, exchange_account_id) ON DELETE RESTRICT",
    "fk_trading_state_operator_request":
        "FOREIGN KEY (operator_request_id, exchange_account_id) REFERENCES "
        "trading_control_requests(request_id, exchange_account_id) ON DELETE RESTRICT",
}


@pytest.fixture
def before(pg_templates: Any, pg_clone: Any) -> Iterator[tuple[str, Engine]]:
    """The previous revision, stamped ``ci``, with two exchange accounts."""
    url = pg_clone(pg_templates.template("drop_realm_before", template_at(_BEFORE)))
    engine = create_engine(url)
    with engine.begin() as conn:
        for account in (_A, _B):
            conn.exec_driver_sql(
                f"INSERT INTO exchange_accounts(id, venue, label) VALUES ('{account}', 'bitfinex', "
                f"'{account[-2:]}')")
    try:
        yield url, engine
    finally:
        engine.dispose()


@pytest.fixture
def upgraded(before: tuple[str, Engine]) -> tuple[str, Engine]:
    url, engine = before
    alembic(url, "upgrade", _REVISION)
    return url, engine


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(text("SELECT version_num FROM alembic_version")))


def _refused(engine: Engine, match: str, *statements: str, role: str | None = None) -> None:
    with pytest.raises(DBAPIError, match=match), engine.begin() as conn:
        if role is not None:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
        for statement in statements:
            conn.exec_driver_sql(statement)


def _archive_digests(engine: Engine) -> dict[str, tuple[int, str]]:
    with engine.connect() as conn:
        tables = conn.execute(text(
            "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ANY(:s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition "
            "ORDER BY 1"), {"s": list(_ARCHIVES)}
        ).scalars().all()
        return {t: tuple(conn.exec_driver_sql(
            "SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(to_jsonb(t)::text, "
            f"E'\\n' ORDER BY to_jsonb(t)::text), ''), 'UTF8')), 'hex') FROM {t} t").one())
            for t in tables}


def _realm(old_schema: bool) -> tuple[str, str]:
    """The realm column and value to write: only the previous revision still has it."""
    return (", deployment_environment", ", 'ci'") if old_schema else ("", "")


def _trading_request(conn: Any, *, account: str = _A, action: str = "kill",
                     old_schema: bool = False) -> str:
    request_id = str(uuid4())
    column, value = _realm(old_schema)
    conn.exec_driver_sql(
        "INSERT INTO trading_control_requests (request_id, exchange_account_id, action, reason, "
        f"requested_by, created_at_ms{column}) VALUES ('{request_id}', '{account}', '{action}', 'x', "
        f"'operator', 1{value})")
    return request_id


def _state(conn: Any, *, account: str = _A, state: str = "HALTED", cause: str = "operator",
           request: str | None = None, old_schema: bool = False) -> int:
    column, value = ("", "") if request is None else (", operator_request_id", f", '{request}'")
    realm_column, realm_value = _realm(old_schema)
    return int(conn.exec_driver_sql(
        "INSERT INTO trading_state (exchange_account_id, state, cause, actor, reason, "
        f"created_at_ms{column}{realm_column}) VALUES ('{account}', '{state}', '{cause}', "
        f"'operator', 'x', 1{value}{realm_value}) RETURNING id").scalar())


# ------------------------------------------------------------------ the upgrade


def test_head_has_no_realm_column_trigger_or_function(pg_head_url: str) -> None:
    engine = create_engine(pg_head_url)
    try:
        with engine.connect() as conn:
            columns = conn.execute(text(
                "SELECT table_schema, table_name FROM information_schema.columns "
                "WHERE column_name = 'deployment_environment' AND table_schema <> ALL(:s)"),
                {"s": list(_ARCHIVES)}).all()
            triggers = conn.scalar(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'database_realm_write'"))
            guard = conn.scalar(text("SELECT to_regprocedure('public.guard_database_realm()')"))
            readers = conn.execute(text(
                "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE p.prosrc ~ 'deployment_environment' AND n.nspname <> ALL(:s)"),
                {"s": list(_ARCHIVES)}).scalars().all()
            rewritten = conn.execute(text(
                "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname || '.' || p.proname = ANY(:f)"), {"f": list(_REWRITTEN)}
            ).scalars().all()
            lock = conn.scalar(text(
                "SELECT prosrc FROM pg_proc "
                "WHERE oid = 'public.guard_trading_state_transition()'::regprocedure"))
    finally:
        engine.dispose()
    assert columns == []
    assert triggers == 0
    assert guard is None
    assert readers == []
    assert sorted(rewritten) == sorted(_REWRITTEN)
    # D6: the trading-state lock key is the account alone.
    assert "'bfx-trading-state:' || NEW.exchange_account_id::text, 0)" in lock


def test_the_upgrade_keeps_rows_and_rebuilds_keys_by_name(before: tuple[str, Engine]) -> None:
    url, engine = before
    with engine.begin() as conn:
        request = _trading_request(conn, old_schema=True)
        state_id = _state(conn, request=request, old_schema=True)
        conn.exec_driver_sql(
            "INSERT INTO nav_peak (account_id, exchange_account_id, deployment_environment, "
            f"symbol, peak, updated_at_ms) VALUES ('a', '{_A}', 'ci', 'fUST', 1, 1)")

    alembic(url, "upgrade", _REVISION)

    with engine.connect() as conn:
        assert conn.scalar(text(
            "SELECT operator_request_id::text FROM trading_state WHERE id = :i"),
            {"i": state_id}) == request
        assert conn.scalar(text("SELECT count(*) FROM nav_peak")) == 1
        keys = dict(conn.execute(text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = ANY(:n)"), {"n": list(_FOREIGN_KEYS)}).tuples().all())
        assert keys == _FOREIGN_KEYS
        assert conn.scalar(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'nav_peak_pkey'"
        )) == "PRIMARY KEY (exchange_account_id, symbol)"
        assert conn.scalar(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_trading_state_evidence'")) == (
            "CHECK (((length(TRIM(BOTH FROM actor)) > 0) AND (length(TRIM(BOTH FROM reason)) > 0) "
            "AND (created_at_ms >= 0)))")
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_sim_venue_event_realm'")) == 0
        assert conn.scalar(text(
            "SELECT to_regclass('public.ix_ledger_observation_query_scope_revision')")) is None
    # The narrow foreign key still refuses a request that does not exist, or one of another
    # account; the narrow primary key still refuses a duplicate.
    _refused(engine, "fk_trading_state_operator_request",
             f"INSERT INTO trading_state (exchange_account_id, state, cause, actor, reason, "
             f"created_at_ms, operator_request_id) VALUES ('{_A}', 'HALTED', 'operator', 'o', 'x', "
             f"2, '{uuid4()}')")
    with engine.begin() as conn:
        unused = _trading_request(conn, action="resume")
    _refused(engine, "fk_trading_state_operator_request",
             f"INSERT INTO trading_state (exchange_account_id, state, cause, actor, reason, "
             f"created_at_ms, operator_request_id) VALUES ('{_B}', 'HALTED', 'operator', 'o', 'x', "
             f"2, '{unused}')")
    _refused(engine, "nav_peak_pkey",
             "INSERT INTO nav_peak (account_id, exchange_account_id, symbol, peak, updated_at_ms) "
             f"VALUES ('a', '{_A}', 'fUST', 2, 2)")


def test_archive_rows_are_unchanged(before: tuple[str, Engine]) -> None:
    url, engine = before
    # One archived row whose column the upgrade must leave alone, planted past the freeze and
    # the realm trigger (replica mode skips both) in this visible statement.
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        target = conn.scalar(text(
            "SELECT format('%I.%I', table_schema, table_name) FROM information_schema.columns "
            "WHERE table_schema = 'legacy_archive' AND column_name = 'deployment_environment' "
            "AND table_name = 'projection_heads'"))
        assert target == "legacy_archive.projection_heads"
        required = conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'legacy_archive' AND table_name = 'projection_heads' "
            "AND is_nullable = 'NO' AND column_default IS NULL ORDER BY ordinal_position")).all()
        samples = {"uuid": f"'{_A}'", "text": "'ci'", "bigint": "1", "integer": "1",
                   "boolean": "false", "jsonb": "'{}'", "numeric": "1",
                   "timestamp with time zone": "now()"}
        columns = ", ".join(name for name, _ in required)
        values = ", ".join(samples[kind] for _, kind in required)
        conn.exec_driver_sql(f"INSERT INTO {target} ({columns}) VALUES ({values})")
    before_digests = _archive_digests(engine)
    assert before_digests["legacy_archive.projection_heads"][0] == 1

    alembic(url, "upgrade", _REVISION)

    assert _archive_digests(engine) == before_digests
    with engine.connect() as conn:
        assert conn.scalar(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'legacy_archive' "
            "AND column_name = 'deployment_environment'")) == 12


def test_a_row_of_another_realm_refuses_the_upgrade(before: tuple[str, Engine]) -> None:
    url, engine = before
    # A ci database whose realm trigger was switched off, in this visible statement, to plant it.
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE nav_peak DISABLE TRIGGER database_realm_write")
        conn.exec_driver_sql(
            "INSERT INTO nav_peak (account_id, exchange_account_id, deployment_environment, "
            f"symbol, peak, updated_at_ms) VALUES ('a', '{_A}', 'prod', 'fUST', 1, 1)")
    with pytest.raises(RuntimeError, match=r"realm other than the database_realm stamp in "
                       r"public\.nav_peak"):
        alembic(url, "upgrade", _REVISION)
    assert _version(engine) == _BEFORE
    with engine.connect() as conn:
        assert conn.scalar(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'nav_peak' "
            "AND column_name = 'deployment_environment'")) == 1


def test_an_archived_event_of_another_realm_refuses_the_upgrade(
        before: tuple[str, Engine]) -> None:
    """The archive read drops its realm filter in this release (D7), so the upgrade refuses
    an event log holding any realm but the stamp's; the archived rows are only read.

    Mutation: drop ``_refuse_other_realms_in_the_read_archive`` from the upgrade and this fails.
    """
    url, engine = before
    # Planted past the freeze and the realm trigger (replica mode skips both), visibly.
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        conn.execute(text(
            "INSERT INTO legacy_archive.event_log (account_id, exchange_account_id, "
            "deployment_environment, event_type, venue_offer_id, cid, payload, occurred_at_ms) "
            "VALUES ('account', :account, 'prod', 'ORDER_FILL', 'legacy-1', 7, '{}', 1)"),
            {"account": _A})
    digests = _archive_digests(engine)
    with pytest.raises(RuntimeError, match=r"1 rows of legacy_archive\.event_log carry a realm"):
        alembic(url, "upgrade", _REVISION)
    assert _version(engine) == _BEFORE and _archive_digests(engine) == digests


def test_an_archived_event_of_the_stamped_realm_is_kept(before: tuple[str, Engine]) -> None:
    url, engine = before
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        conn.execute(text(
            "INSERT INTO legacy_archive.event_log (account_id, exchange_account_id, "
            "deployment_environment, event_type, venue_offer_id, cid, payload, occurred_at_ms) "
            "VALUES ('account', :account, 'ci', 'ORDER_FILL', 'legacy-1', 7, '{}', 1)"),
            {"account": _A})
    digests = _archive_digests(engine)
    alembic(url, "upgrade", _REVISION)
    assert _archive_digests(engine) == digests
    assert digests["legacy_archive.event_log"][0] == 1


def test_a_statement_of_the_previous_version_cannot_run(before: tuple[str, Engine]) -> None:
    """The advisory-lock keys lost the environment in this release, so a program of the
    previous version takes a different key and no longer excludes the new one. What keeps
    it from writing is the schema: every statement that names the column is refused, on
    every table that had it, before any row is read or changed."""
    url, engine = before
    with engine.connect() as conn:
        tables = conn.execute(text(
            "SELECT table_schema, table_name FROM information_schema.columns "
            "WHERE column_name = 'deployment_environment' AND table_schema <> ALL(:s) "
            "ORDER BY 1, 2"), {"s": list(_ARCHIVES)}).all()
        loose = conn.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name = 'deployment_environment' AND table_schema <> ALL(:s) "
            "AND (is_nullable <> 'NO' OR column_default IS NOT NULL)"),
            {"s": list(_ARCHIVES)}).scalars().all()
    assert len(tables) == 31
    # The other direction: on the previous schema the column is required and has no
    # default, so an INSERT of this version (which does not name it) is refused too:
    # by the realm trigger first, and by NOT NULL behind it.
    assert loose == []
    _refused(engine, "refuses a write of realm <NULL> to capital_command_clock",
             f"INSERT INTO capital_command_clock (exchange_account_id, revision) VALUES ('{_A}', 0)")
    alembic(url, "upgrade", _REVISION)
    statements = [
        statement
        for schema, table in tables
        for name in [f'"{schema}"."{table}"']
        for statement in (
            f"INSERT INTO {name} (deployment_environment) VALUES ('ci')",
            f"UPDATE {name} SET deployment_environment = 'ci'",
            f"SELECT 1 FROM {name} WHERE deployment_environment = 'ci'",
            f"DELETE FROM {name} WHERE deployment_environment = 'ci'",
        )
    ]
    # The clock upsert as the previous version wrote it (conflict target included).
    statements.append(
        "INSERT INTO capital_command_clock (exchange_account_id, deployment_environment, "
        f"revision) VALUES ('{_A}', 'ci', 0) "
        "ON CONFLICT (exchange_account_id, deployment_environment) DO NOTHING")
    for statement in statements:
        with pytest.raises(DBAPIError) as refused, engine.begin() as conn:
            conn.exec_driver_sql(statement)
        assert getattr(refused.value.orig, "sqlstate", None) == "42703", statement


def test_the_downgrade_refuses(upgraded: tuple[str, Engine]) -> None:
    url, engine = upgraded
    with pytest.raises(RuntimeError, match="irreversible"):
        alembic(url, "downgrade", _BEFORE)
    assert _version(engine) == _REVISION


# ------------------------------------------------------------------ the rewritten guards


def test_trading_state_transitions_are_still_guarded(upgraded: tuple[str, Engine]) -> None:
    _, engine = upgraded
    _refused(engine, "illegal trading state transition none -> ACTIVE by auto",
             f"INSERT INTO trading_state (exchange_account_id, state, cause, actor, reason, "
             f"created_at_ms) VALUES ('{_A}', 'ACTIVE', 'auto', 'bot', 'x', 1)")
    with engine.begin() as conn:
        _state(conn, account=_A, state="HALTED", cause="operator")
    # Account B has no history: its auto resume is still refused (the latest row is per account).
    _refused(engine, "illegal trading state transition none -> ACTIVE by auto",
             f"INSERT INTO trading_state (exchange_account_id, state, cause, actor, reason, "
             f"created_at_ms) VALUES ('{_B}', 'ACTIVE', 'auto', 'bot', 'x', 1)")
    # A's latest row is an operator halt: an auto resume is refused, an operator resume is not.
    _refused(engine, r"illegal trading state transition HALTED/operator -> ACTIVE by auto",
             f"INSERT INTO trading_state (exchange_account_id, state, cause, actor, reason, "
             f"created_at_ms) VALUES ('{_A}', 'ACTIVE', 'auto', 'bot', 'x', 1)")
    with engine.begin() as conn:
        _state(conn, account=_A, state="ACTIVE", cause="operator")


def test_the_archive_run_guard_still_freezes_the_account(upgraded: tuple[str, Engine]) -> None:
    _, engine = upgraded
    # A run is captured and completed in one transaction (projection_audit.require_complete).
    def insert(run: str) -> str:
        return ("INSERT INTO projection_audit.runs (run_id, exchange_account_id, manifest) "
                f"VALUES ('{run}', '{_A}', '\\x00')")

    refused = str(uuid4())
    _refused(engine, "archive only permits completion", insert(refused),
             f"UPDATE projection_audit.runs SET exchange_account_id = '{_B}', complete = true "
             f"WHERE run_id = '{refused}'")
    completed = str(uuid4())
    with engine.begin() as conn:
        conn.exec_driver_sql(insert(completed))
        conn.exec_driver_sql(
            f"UPDATE projection_audit.runs SET complete = true WHERE run_id = '{completed}'")


def test_runtime_policy_guards_compare_the_account(upgraded: tuple[str, Engine]) -> None:
    _, engine = upgraded
    revision_a, revision_b = str(uuid4()), str(uuid4())
    with engine.begin() as conn:
        for revision_id, account in ((revision_a, _A), (revision_b, _B)):
            conn.exec_driver_sql(
                "INSERT INTO capital_policy_revisions (id, exchange_account_id, symbol, revision, "
                f"schema_version, policy, digest, source) VALUES ('{revision_id}', '{account}', "
                "'fUST', 1, 1, '{\"enabled\": true}', 'd', '{}')")
            conn.exec_driver_sql(
                "INSERT INTO capital_policy_heads (exchange_account_id, symbol, revision_id, "
                f"revision) VALUES ('{account}', 'fUST', '{revision_id}', 1)")
        # A role that is not the owner: the guards bypass the owner (the amendment script).
        conn.exec_driver_sql("CREATE ROLE drop_realm_writer NOLOGIN")
        conn.exec_driver_sql(
            "GRANT SELECT, INSERT, UPDATE ON capital_policy_revisions, capital_policy_heads, "
            "capital_policy_requests TO drop_realm_writer")
        conn.exec_driver_sql(
            "GRANT EXECUTE ON FUNCTION public.operator_authorized(uuid, text) TO drop_realm_writer")
    def revision(revision_id: str, account: str, number: int, enabled: bool,
                 request: str | None = None) -> str:
        request_value = "NULL" if request is None else f"'{request}'"
        return ("INSERT INTO capital_policy_revisions (id, exchange_account_id, symbol, revision, "
                "schema_version, policy, digest, source, operator_request_id) VALUES "
                f"('{revision_id}', '{account}', 'fUST', {number}, 1, "
                f"'{{\"enabled\": {str(enabled).lower()}}}', 'd', '{{}}', {request_value})")

    # Owner rows (the guards let the owner through): B's revision 2, and A's revision 2.
    b_second, a_second = str(uuid4()), str(uuid4())
    with engine.begin() as conn:
        conn.exec_driver_sql(revision(b_second, _B, 2, True))
        conn.exec_driver_sql(revision(a_second, _A, 2, True))
    # A's head may not advance to B's revision 2: only the account differs.
    _refused(engine, "may only advance to the next revision of its scope",
             f"UPDATE capital_policy_heads SET revision_id = '{b_second}', revision = 2 "
             f"WHERE exchange_account_id = '{_A}'", role="drop_realm_writer")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE drop_realm_writer")
        conn.exec_driver_sql(
            f"UPDATE capital_policy_heads SET revision_id = '{a_second}', revision = 2 "
            f"WHERE exchange_account_id = '{_A}'")
    b_request = str(uuid4())
    # A waiting disable request of account B, by an operator authorized on B (and on A), does
    # not authorize A's revision 3.
    with engine.begin() as conn:
        for account in (_A, _B):
            conn.exec_driver_sql(
                "INSERT INTO exchange_account_memberships(exchange_account_id, user_id, role) "
                f"VALUES ('{account}', 'operator', 'owner')")
        conn.exec_driver_sql(
            "INSERT INTO capital_policy_requests (request_id, exchange_account_id, symbol, action, "
            f"reason, requested_by, created_at_ms) VALUES ('{b_request}', '{_B}', 'fUST', "
            "'disable', 'x', 'operator', 1)")
    _refused(engine, "must apply a waiting request of an authorized operator",
             revision(str(uuid4()), _A, 3, False, b_request), role="drop_realm_writer")
    # A revision that skips one is refused before the request is looked at.
    _refused(engine, "may only toggle enabled of the current revision",
             revision(str(uuid4()), _A, 4, False), role="drop_realm_writer")
