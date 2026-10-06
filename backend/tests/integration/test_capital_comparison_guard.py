"""Cutover-reader attestation and reverse scope inventory on a migrated PostgreSQL.

Every test runs against a real migrated database: the LOGIN is a ``NOINHERIT`` member of
``bfx_cutover_reader`` (the runbook shape), the guard does ``SET LOCAL ROLE`` to the group
and the privilege scans read the real catalogs.

Mutations (apply one at a time, run this file, revert; the test that fails is named):

* drop a ``_PRIVILEGE_SCANS`` entry (each of the eight): ``test_each_unsafe_shape_has_its_own_reason``
  fails for the dropped scan's parameter;
* drop the role-closure check: ``test_each_unsafe_shape_has_its_own_reason[role_closure_unexpected]``;
* drop the role-attribute check: ``...[role_attribute_privileged]``;
* skip ``SET LOCAL ROLE``: ``test_runbook_shape_passes_the_attestation`` (``reader_role_unavailable``);
* make the reverse inventory ignore ``capital_policy_heads``:
  ``test_unlisted_policy_head_is_reported`` and ``test_every_authority_table_is_inventoried``.
"""

import io
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.apps import capital_comparison as command
from bfx_funding_bot.apps.capital_comparison_guard import (
    READER_ROLE,
    ConnectionPlan,
    GuardRejectedError,
    verify_connection,
)
from bfx_funding_bot.apps.capital_comparison_inventory import (
    LEDGER_SCOPE_TABLES,
    LEGACY_SCOPE_SOURCES,
    InventoryResult,
    reverse_inventory,
)
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.capital_observed_baseline import (
    OBSERVATION_FORMAT,
    OBSERVATION_VERSION,
)
from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from bfx_funding_bot.modules.execution.legacy_archive import qualified
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES
from bfx_funding_bot.modules.trading import CapitalScope
from tests.integration.test_capital_repository import repository, setup_policy, snapshot


def _orm_key(table: str) -> str:
    """The metadata key of ``table``: archived tables carry their schema (c2d3e4f5a6b7)."""
    return qualified(table).removeprefix("public.")

pytestmark = pytest.mark.integration

CELLS = Path(__file__).parents[2] / "configs/cells.live.yaml"
PASSWORD = "test-only"
ENVIRONMENT = "ci"


@dataclass
class World:
    url: URL
    factory: async_sessionmaker[AsyncSession]
    account: UUID
    logins: list[str]

    def exec(self, sql: str) -> None:
        """Owner statement on this test's own clone."""
        engine = create_engine(self.url.set(drivername="postgresql+psycopg"))
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(sql)
        finally:
            engine.dispose()

    def login(self, *, member: bool = True) -> str:
        name = "attest_" + uuid4().hex[:12]
        self.logins.append(name)
        self.exec(
            f'CREATE ROLE "{name}" LOGIN PASSWORD \'{PASSWORD}\' NOSUPERUSER NOCREATEDB '
            "NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION"
        )
        if member:
            self.exec(f'GRANT {READER_ROLE} TO "{name}"')
        return name

    def scopes(self, account: UUID | None = None, symbol: str = "fUST") -> list[CapitalScope]:
        return [
            CapitalScope(account or self.account, ENVIRONMENT, symbol, f"{symbol}_{cell}")
            for cell in ("a30", "p2")
        ]

    def engine_as(self, login: str):  # type: ignore[no-untyped-def]
        return create_async_engine(
            self.url.set(drivername="postgresql+asyncpg", username=login, password=PASSWORD)
        )


@pytest_asyncio.fixture
async def world(pg_head_url):  # type: ignore[no-untyped-def]
    url = make_url(pg_head_url)
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="guard"))
    repo = repository(account)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    made = World(url, factory, account, [])
    try:
        yield made
    finally:
        for name in made.logins:
            made.exec(f'DROP OWNED BY "{name}"')
            made.exec(f'DROP ROLE "{name}"')
        await engine.dispose()


async def attest(world: World, login: str) -> None:
    plan = ConnectionPlan(world.url, login, world.url.database or "", "t", 1, "cutover")
    engine = world.engine_as(login)
    try:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            await verify_connection(session, plan)
    finally:
        await engine.dispose()


async def inventory(
    world: World,
    scopes: list[CapitalScope],
    declared: frozenset[tuple[UUID, str, str]] = frozenset(),
) -> InventoryResult:
    """The real inventory, as the attested LOGIN under ``SET LOCAL ROLE`` (real column grants)."""
    engine = world.engine_as(world.login())
    try:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            await session.execute(text(f"SET LOCAL ROLE {READER_ROLE}"))
            return await reverse_inventory(session, scopes=scopes, policy_without_cell=declared)
    finally:
        await engine.dispose()


def args(tmp_path: Path, world: World, login: str) -> list[str]:
    """Cutover argv with well-formed inputs: a runner observation of the world's account
    (as-of 1060) and a committed seed's evidence (ids that need not exist: these tests stop at
    the attestation)."""
    url = world.url.set(username=login, password=PASSWORD)
    manifest = tmp_path / "cutover.json"
    manifest.write_text(
        json.dumps(
            {
                "mode": "cutover",
                "host": url.host,
                "port": url.port,
                "database": url.database,
                "user": login,
                "run_id": "integration",
                "now_ms": 1060,
            }
        )
    )
    dsn_file = tmp_path / "dsn"
    dsn_file.write_text(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    dsn_file.chmod(0o600)
    event = VenueSnapshotObserved(
        account_id=str(world.account), environment=ENVIRONMENT, query_started_at_ms=1000,
        query_finished_at_ms=1050, offers=(), credits=(), wallet_available={"fUST": Decimal(1)},
        coverage=SnapshotCoverage(True, True, True),
    )
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps({
        "format": OBSERVATION_FORMAT, "version": OBSERVATION_VERSION,
        "observations": [{
            "account_id": str(world.account), "environment": ENVIRONMENT,
            "event": serialize_event(event),
            "confirmation": serialize_event(replace(
                event, query_started_at_ms=1050, query_finished_at_ms=1060, event_id=uuid4())),
            "ledger": {"query_id": str(uuid4()), "observation_id": str(uuid4()),
                       "first_digest": "0" * 64, "confirmation_digest": "0" * 64},
        }],
    }))
    scope = {"account_id": str(world.account), "environment": ENVIRONMENT}
    evidence = tmp_path / "seed.jsonl"
    evidence.write_text("".join(json.dumps(line) + "\n" for line in (
        {"kind": "seed", "scope": scope, "observation_id": str(uuid4()), "basis_id": str(uuid4()),
         "watermarks": {"legacy_final_event_seq": 999_999, "snapshot_event_seq": 999_999,
                        "snapshot_query_id": str(uuid4()), "trading_state_max_id": None},
         "failed_uncertainty_requests": [],
         "carried_requests": {"capital_policy_requests": [], "trading_control_requests": []}},
        {"kind": "verification", "scope": scope, "mismatches": []},
        {"kind": "summary", "exit_code": 0, "committed": True},
    )))
    return [
        "--mode", "cutover", "--authorize-cutover-read", "--cutover-manifest", str(manifest),
        "--dsn-file", str(dsn_file), "--run-id", "integration", "--code-revision", "integration",
        "--scope", f"{world.account}:{ENVIRONMENT}", "--cells", str(CELLS),
        "--observation", str(observation), "--seed-evidence", str(evidence),
    ]  # fmt: skip


async def run_command(
    tmp_path: Path, world: World, login: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[dict[str, object]]]:
    monkeypatch.setattr(command.time, "time_ns", lambda: 2_000_000_000)
    output = io.StringIO()
    code = await command.run(args(tmp_path, world, login), output=output)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


# --- the runbook shape ---------------------------------------------------------------------------


async def test_runbook_shape_reaches_the_arms_without_permission_errors(
    world, tmp_path, monkeypatch
) -> None:
    """The command gets past attestation and inventory and runs both cutover arms. This world
    has no seed and no ledger observation, so the run fails on evidence, never on a privilege."""
    code, rows = await run_command(tmp_path, world, world.login(), monkeypatch)
    assert code == 1, rows[-1]
    assert rows[0]["kind"] == "inventory" and rows[0]["status"] == "ok"
    arms = [row for row in rows if row["kind"] == "arm"]
    # No runner observation exists in this world, so the file binds to nothing.
    assert [(row["status"], row["reason"]) for row in arms] == [
        ("not_comparable", "observation_binding_mismatch")] * 2
    closure = [row for row in rows if row["kind"] == "closure"]
    assert {v["reason"] for row in closure for v in row["violations"]} == {
        "seed_observation_mismatch", "seed_basis_mismatch", "legacy_final_snapshot_moved",
        "legacy_stream_moved", "seed_anchor_unavailable",
    }
    assert "error" not in rows[-1]["arms"]["closure"]


async def test_runbook_shape_passes_the_attestation(world) -> None:
    await attest(world, world.login())


async def test_owner_is_rejected_by_the_actual_closure_check(world, tmp_path, monkeypatch) -> None:
    owner = world.url.username
    url = world.url
    manifest = tmp_path / "owner.json"
    manifest.write_text(
        json.dumps({"mode": "cutover", "host": url.host, "port": url.port,
                    "database": url.database, "user": owner, "run_id": "integration",
                    "now_ms": 1060})
    )  # fmt: skip
    dsn_file = tmp_path / "owner-dsn"
    dsn_file.write_text(url.set(drivername="postgresql").render_as_string(hide_password=False))
    dsn_file.chmod(0o600)
    argv = args(tmp_path, world, world.login())
    argv[argv.index("--cutover-manifest") + 1] = str(manifest)
    argv[argv.index("--dsn-file") + 1] = str(dsn_file)
    monkeypatch.setattr(command.time, "time_ns", lambda: 2_000_000_000)
    output = io.StringIO()
    assert await command.run(argv, output=output) == 3
    summary = json.loads(output.getvalue())
    assert summary["reason"] == "role_closure_unexpected" and summary["exit_code"] == 3


_TRADING_STATE = "public.trading_state"
_OTHER = "CREATE SCHEMA other_schema; "
# (case id, reason, setup SQL with {login}/{db}, a name the detail must carry)
_UNSAFE: list[tuple[str, str, str, str]] = [
    ("table_insert", "table_write_privilege",
     "GRANT INSERT ON public.trading_state TO {login}", _TRADING_STATE),
    ("table_trigger", "table_write_privilege",
     "GRANT TRIGGER ON public.trading_state TO {login}", _TRADING_STATE),
    ("table_maintain", "table_write_privilege",
     "GRANT MAINTAIN ON public.trading_state TO {login}", _TRADING_STATE),
    # The web users, roles and credentials live in the auth schema: a write there forges the operator path.
    ("auth_account_insert", "table_write_privilege",
     "GRANT INSERT ON auth.account TO {login}", "auth.account"),
    ("auth_user_role_update", "column_write_privilege",
     'GRANT UPDATE (role) ON auth."user" TO {login}', "auth.user"),
    ("other_schema_insert", "table_write_privilege",
     _OTHER + "CREATE TABLE other_schema.t(x int); GRANT INSERT ON other_schema.t TO {login}",
     "other_schema.t"),
    ("column_update", "column_write_privilege",
     "GRANT UPDATE (state) ON public.trading_state TO {login}", _TRADING_STATE),
    ("sequence_usage", "sequence_privilege",
     "GRANT USAGE ON SEQUENCE public.trading_state_id_seq TO {login}",
     "public.trading_state_id_seq"),
    ("database_create", "database_create_privilege",
     'GRANT CREATE ON DATABASE "{db}" TO {login}', "{db}"),
    ("schema_create", "schema_create_privilege",
     "GRANT CREATE ON SCHEMA public TO {login}", "public"),
    ("closure", "role_closure_unexpected",
     "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bfx_bot') "
     "THEN CREATE ROLE bfx_bot NOLOGIN; END IF; END $$; GRANT bfx_bot TO {login}", "bfx_bot"),
    ("superuser", "role_attribute_privileged", "ALTER ROLE {login} SUPERUSER", "{login}"),
    ("login_inherits", "login_inherits", "ALTER ROLE {login} INHERIT", "{login}"),
    # PG16+: inheritance is per grant, so a NOINHERIT LOGIN can still inherit through one grant.
    ("login_grant_inherits", "login_inherits",
     "REVOKE bfx_cutover_reader FROM {login}; "
     "GRANT bfx_cutover_reader TO {login} WITH INHERIT TRUE, SET TRUE", "{login}"),
    ("reader_can_login", "reader_can_login", "ALTER ROLE bfx_cutover_reader LOGIN",
     "bfx_cutover_reader"),
    ("security_definer", "security_definer_executable",
     "CREATE FUNCTION public.guard_probe() RETURNS int LANGUAGE sql SECURITY DEFINER "
     "AS 'SELECT 1'; REVOKE ALL ON FUNCTION public.guard_probe() FROM PUBLIC; "
     "GRANT EXECUTE ON FUNCTION public.guard_probe() TO {login}", "public.guard_probe"),
    # Ownership always brings its own privileges; revoke them so only the ownership scan sees it.
    ("owned_relation", "owned_relation",
     _OTHER + "CREATE TABLE other_schema.owned(x int); "
     "ALTER TABLE other_schema.owned OWNER TO {login}; "
     "REVOKE ALL ON other_schema.owned FROM {login}", "other_schema.owned"),
    ("owned_function", "owned_function",
     _OTHER + "CREATE FUNCTION other_schema.owned() RETURNS int LANGUAGE sql AS 'SELECT 1'; "
     "ALTER FUNCTION other_schema.owned() OWNER TO {login}", "other_schema.owned"),
    ("owned_schema", "owned_schema",
     "CREATE SCHEMA other_schema AUTHORIZATION {login}; "
     "REVOKE ALL ON SCHEMA other_schema FROM {login}", "other_schema"),
    ("forbidden_extension", "forbidden_extension", "CREATE EXTENSION dblink", "dblink"),
]  # fmt: skip


@pytest.mark.parametrize(("case", "reason", "setup", "named"), _UNSAFE, ids=[c[0] for c in _UNSAFE])
async def test_each_unsafe_shape_has_its_own_reason(world, case, reason, setup, named) -> None:
    login = world.login()
    database = world.url.database
    if case == "forbidden_extension":
        probe = create_engine(world.url.set(drivername="postgresql+psycopg"))
        try:
            with probe.connect() as conn:
                available = conn.exec_driver_sql(
                    "SELECT count(*) FROM pg_available_extensions WHERE name = 'dblink'"
                ).scalar()
        finally:
            probe.dispose()
        if not available:
            pytest.skip("dblink is not installed in this PostgreSQL")
    world.exec(setup.format(login=login, db=database))
    try:
        with pytest.raises(GuardRejectedError) as raised:
            await attest(world, login)
    finally:
        if case == "reader_can_login":
            world.exec("ALTER ROLE bfx_cutover_reader NOLOGIN")  # the role is cluster-wide
    assert raised.value.reason == reason
    assert named.format(login=login, db=database) in raised.value.detail


async def test_a_direct_login_without_membership_cannot_switch_role(world) -> None:
    with pytest.raises(GuardRejectedError) as raised:
        await attest(world, world.login(member=False))
    assert raised.value.reason == "reader_role_unavailable"


async def test_safe_neighbours_pass(world) -> None:
    """Near misses of each unsafe shape stay accepted: no false positives on the real schema."""
    login = world.login()
    world.exec(
        # SECURITY DEFINER without EXECUTE for the reachable roles; SELECT (not write) on web
        # sessions and on another schema; SELECT on a sequence is not USAGE; USAGE on a schema
        # and TEMPORARY on the database (the restore drill grants it) are not CREATE.
        "CREATE FUNCTION public.guard_hidden() RETURNS int LANGUAGE sql SECURITY DEFINER "
        "AS 'SELECT 1'; REVOKE ALL ON FUNCTION public.guard_hidden() FROM PUBLIC; "
        "CREATE SCHEMA other_schema; CREATE TABLE other_schema.t(x int); "
        f'GRANT USAGE ON SCHEMA other_schema TO "{login}"; '
        f'GRANT SELECT ON other_schema.t TO "{login}"; '
        f'GRANT SELECT ON auth.account TO "{login}"; '
        f'GRANT TEMPORARY ON DATABASE "{world.url.database}" TO "{login}"; '
        f'GRANT SELECT ON SEQUENCE public.trading_state_id_seq TO "{login}"'
    )
    await attest(world, login)


# --- reverse policy-scope inventory ---------------------------------------------------------------


async def test_complete_scopes_have_no_violation(world) -> None:
    assert await inventory(world, world.scopes()) == InventoryResult("ok", ())


async def test_unlisted_policy_head_is_reported(world) -> None:
    other = uuid4()
    async with world.factory.begin() as session:
        session.add(ExchangeAccount(id=other, venue="bitfinex", label="other"))
    await setup_policy(world.factory, repository(other))
    result = await inventory(world, world.scopes())
    assert result.status == "not_comparable"
    assert result.violations == (
        {
            "reason": "scope_unlisted",
            "table": "capital_policy_heads",
            "account_id": str(other),
            "environment": ENVIRONMENT,
        },
    )


async def test_policy_symbol_without_cell_unless_declared(world) -> None:
    only_usd = world.scopes(symbol="fUSD")
    result = await inventory(world, only_usd)
    assert [v["reason"] for v in result.violations] == ["policy_symbol_without_cell"]
    assert result.violations[0]["symbol"] == "fUST"
    declared = frozenset({(world.account, ENVIRONMENT, "fUST")})
    assert (await inventory(world, only_usd, declared)).status == "ok"


async def test_live_legacy_credit_symbol_needs_a_cell(world) -> None:
    other = uuid4()
    async with world.factory.begin() as session:
        session.add(ExchangeAccount(id=other, venue="bitfinex", label="lending"))
    repo = repository(other)
    await setup_policy(world.factory, repo)
    credit = VenueCreditObservation("c1", "fUST", Decimal("300"), Decimal("0.0001"), 2, "active")
    await snapshot(world.factory, repo, "700", credits=(credit,))
    scopes = [*world.scopes(), *world.scopes(other, symbol="fUSD")]
    declared = frozenset({(other, ENVIRONMENT, "fUST")})  # the head is declared; the credit is not
    result = await inventory(world, scopes, declared)
    assert [(v["reason"], v["symbol"]) for v in result.violations] == [
        ("live_symbol_without_cell", "fUST")
    ]
    assert (await inventory(world, [*world.scopes(), *world.scopes(other)])).status == "ok"


# Every table the inventory reads, each with one planted row (all CHECKs and FKs bypassed on the
# scratch clone: only the scope, and the state the row filter selects on, matter here).
_FILTER_STATES = {
    "execution_uncertainties": ("open", "resolved"),
    "offer_claims": ("claimed", "released"),
    "uncertainty_resolution_requests": ("requested", "applied"),
    "capital_policy_requests": ("requested", "applied"),
    "trading_control_requests": ("requested", "applied"),
}
# Spelled out (not read from the module) so dropping a source from the inventory fails here.
_ALL_SOURCES = [
    "capital_policy_heads", "capital_snapshots", "submission_attempts", "execution_uncertainties",
    "offer_claims", "trading_state", "uncertainty_resolution_requests", "capital_policy_requests",
    "trading_control_requests", "capital_command_clock", "ledger_observation_query",
    "ledger_observation", "venue_offer_mirror", "venue_credit_mirror",
    "submission_attempt_journal", "quarantine_opening", "execution_resolution_journal",
    "accepted_capital_basis",
]  # fmt: skip


def _plant(world: World, table: str, account: UUID, state: str | None) -> None:
    """One row of ``table`` for (account, ci); every other required column gets a dummy value."""
    engine = create_engine(world.url.set(drivername="postgresql+psycopg"))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("SET session_replication_role = replica")
            for name in conn.exec_driver_sql(
                "SELECT conname FROM pg_constraint WHERE contype = 'c' "
                f"AND conrelid = '{qualified(table)}'::regclass"
            ).scalars().all():
                conn.exec_driver_sql(f'ALTER TABLE {qualified(table)} DROP CONSTRAINT "{name}"')
            columns = conn.exec_driver_sql(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                f"WHERE a.attrelid = '{qualified(table)}'::regclass AND a.attnum > 0 "
                "AND NOT a.attisdropped AND a.attgenerated = '' AND a.attidentity = '' "
                "AND (a.attnotnull AND d.adbin IS NULL "
                "OR a.attname IN ('exchange_account_id', 'deployment_environment', 'state'))"
            ).all()
            amount_json = "'" + json.dumps({"amount": "0"}) + "'"
            dummies = {"boolean": "false", "uuid": f"'{uuid4()}'", "jsonb": amount_json}
            names, values = [], []
            for name, kind in columns:
                names.append(name)
                if name == "exchange_account_id":
                    values.append(f"'{account}'")
                elif name == "deployment_environment":
                    values.append(f"'{ENVIRONMENT}'")
                elif name == "state":
                    values.append(f"'{state or 'x'}'")
                elif kind in dummies:
                    values.append(dummies[kind])
                elif kind in {"bigint", "integer", "smallint"} or kind.startswith("numeric"):
                    values.append("0")
                elif kind == "bytea":
                    values.append("'\\x00'")
                else:
                    values.append("'x'")
            conn.exec_driver_sql(
                f"INSERT INTO {qualified(table)} ({', '.join(f'\"{n}\"' for n in names)}) "
                f"VALUES ({', '.join(values)})"
            )
    finally:
        engine.dispose()


def _clear(world: World, table: str, account: UUID) -> None:
    world.exec(
        "SET session_replication_role = replica; "
        f"DELETE FROM {qualified(table)} WHERE exchange_account_id = '{account}'"
    )


async def test_every_authority_table_is_inventoried(world) -> None:
    """Each source reports an unlisted scope, and ignores listed scopes and filtered-out rows."""
    stranger = uuid4()
    async with world.factory.begin() as session:
        session.add(ExchangeAccount(id=stranger, venue="bitfinex", label="stranger"))
    # One row per table is enough: a second clone per table would only repeat the setup.
    reported: set[str] = set()
    for table in _ALL_SOURCES:
        open_state, closed_state = _FILTER_STATES.get(table, (None, None))
        if closed_state is not None:
            _plant(world, table, stranger, closed_state)
            assert (await inventory(world, world.scopes())).status == "ok", table
            _clear(world, table, stranger)
        _plant(world, table, stranger, open_state)
        result = await inventory(world, world.scopes())
        reported |= {v["table"] for v in result.violations if v["reason"] == "scope_unlisted"}
        assert table in reported, table
        _clear(world, table, stranger)
    assert reported >= set(_ALL_SOURCES)


async def test_ledger_scope_tables_cover_every_scope_bearing_ledger_table() -> None:
    scoped = {
        table.name
        for table in LEDGER_TABLES
        if {"exchange_account_id", "deployment_environment"} <= {c.name for c in table.columns}
    }
    assert scoped == set(LEDGER_SCOPE_TABLES)
    assert set(_ALL_SOURCES) == {t for t, _ in LEGACY_SCOPE_SOURCES} | set(LEDGER_SCOPE_TABLES)


async def test_missing_inventory_table_is_a_violation_not_a_skip(world) -> None:
    world.exec("ALTER TABLE public.trading_control_requests RENAME TO trading_control_requests_x")
    result = await inventory(world, world.scopes())
    assert {"reason": "inventory_table_missing", "table": "trading_control_requests"} in (
        result.violations
    )


# --- the reader's grants -------------------------------------------------------------------------

# Whole ORM rows: the baseline (b5c6d7e8f9a0) and the cutover legacy arm's ``_classify``
# (offer_claims, funding_trades: d7e8f9a0b1c2).
_LEGACY_READ = (
    "event_log", "event_prefix_hashes", "capital_policy_heads", "capital_policy_revisions",
    "capital_snapshots", "capital_snapshot_queries", "execution_decisions", "projection_heads",
    "submission_attempts", "execution_uncertainties", "offer_claims", "funding_trades",
)  # fmt: skip
_SCOPE = ("exchange_account_id", "deployment_environment")
_SCOPE_AND_STATE = (*_SCOPE, "state")
# The inventory's scope/state columns plus what the closure verifier reads (d7e8f9a0b1c2).
_INVENTORY_READ = {
    "trading_state": {*_SCOPE, "id"},
    "uncertainty_resolution_requests": {*_SCOPE_AND_STATE, "request_id", "outcome_reason"},
    "capital_policy_requests": {*_SCOPE_AND_STATE, "request_id"},
    "trading_control_requests": {*_SCOPE_AND_STATE, "request_id"},
    "venue_offer_state": {*_SCOPE, "symbol", "is_terminal"},
    "venue_credit_state": {*_SCOPE, "symbol", "is_terminal"},
}
_DENIED = (
    ("ledger_observation", "evidence"),
    ("ledger_observation_offer", "raw"),
    ("submission_attempt_journal", "authorization_evidence"),
    ("transport_outcome_journal", "evidence"),
    ("quarantine_opening", "evidence"),
    ("execution_resolution_journal", "evidence"),
)


def _reader_columns(world: World) -> dict[str, set[str]]:
    engine = create_engine(world.url.set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT c.relname, a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "WHERE n.nspname IN ('public', 'legacy_archive') AND c.relkind = 'r' "
                f"AND has_column_privilege('{READER_ROLE}', c.oid, a.attnum, 'SELECT')"
            ).all()
            tables = conn.exec_driver_sql(
                "SELECT table_name FROM information_schema.role_table_grants "
                f"WHERE grantee = '{READER_ROLE}' AND table_schema IN ('public', 'legacy_archive')"
            ).scalars().all()
            assert tables == [], "the group holds no table-level grant"
            writes = conn.exec_driver_sql(
                "SELECT c.relname, a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "WHERE n.nspname IN ('public', 'legacy_archive') AND c.relkind = 'r' "
                f"AND has_column_privilege('{READER_ROLE}', c.oid, a.attnum, 'INSERT,UPDATE')"
            ).all()
            assert writes == []
    finally:
        engine.dispose()
    found: dict[str, set[str]] = {}
    for table, column in rows:
        found.setdefault(table, set()).add(column)
    return found


async def test_reader_grants_are_columns_only_and_exact(world) -> None:
    """Pins the comparison's legacy and inventory grants (b5c6d7e8f9a0, d7e8f9a0b1c2).

    The baseline loads whole ORM rows of the legacy tables, so the reader holds every mapped
    column of them; the inventory adds only scope and state columns of its other tables.
    """
    found = _reader_columns(world)
    for table in _LEGACY_READ:
        assert found[table] == {c.name for c in Base.metadata.tables[_orm_key(table)].columns}, table
    for table, columns in _INVENTORY_READ.items():
        assert found[table] == columns, table
    for table, column in _DENIED:
        assert column not in found.get(table, set()), (table, column)
    assert {"conservation", "lent_unexplained", "foreign_executed", "fill_conflicts"} <= (
        found["accepted_capital_basis_symbol"]
    )
    assert {"intended_amount", "normalized_payload", "seed_provenance"} <= (
        found["submission_attempt_journal"]
    )
    assert "origin" in found["ledger_observation"]
