"""Cutover-reader attestation and reverse scope inventory on a migrated PostgreSQL.

Every test runs against a real migrated database: the LOGIN is a ``NOINHERIT`` member of
``bfx_cutover_reader`` (the runbook shape), the guard does ``SET LOCAL ROLE`` to the group
and the privilege scans read the real catalogs.

Mutations (apply one at a time, run this file, revert; the test that fails is named):

* drop a ``_PRIVILEGE_SCANS`` entry (each of the eight): ``test_each_unsafe_shape_has_its_own_reason``
  fails for the dropped scan's parameter;
* drop the role-closure check: ``test_each_unsafe_shape_has_its_own_reason[role_closure_unexpected]``;
* drop the role-attribute check: ``...[role_attribute_privileged]``;
* skip ``SET LOCAL ROLE``: ``test_runbook_shape_passes_the_full_command`` (``reader_role_unavailable``);
* make the reverse inventory ignore ``capital_policy_heads``:
  ``test_unlisted_policy_head_is_reported`` and ``test_every_authority_table_is_inventoried``.
"""

import io
import json
from dataclasses import dataclass
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
from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES
from bfx_funding_bot.modules.trading import CapitalScope
from tests.integration.test_capital_repository import repository, setup_policy, snapshot

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
            }
        )
    )
    dsn_file = tmp_path / "dsn"
    dsn_file.write_text(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    dsn_file.chmod(0o600)
    return [
        "--mode", "cutover", "--authorize-cutover-read", "--cutover-manifest", str(manifest),
        "--dsn-file", str(dsn_file), "--run-id", "integration", "--code-revision", "integration",
        "--scope", f"{world.account}:{ENVIRONMENT}", "--cells", str(CELLS),
    ]  # fmt: skip


async def run_command(
    tmp_path: Path, world: World, login: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[dict[str, object]]]:
    monkeypatch.setattr(command.time, "time_ns", lambda: 2_000_000_000)
    output = io.StringIO()
    code = await command.run(args(tmp_path, world, login), output=output)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


# --- the runbook shape ---------------------------------------------------------------------------


async def test_runbook_shape_passes_the_full_command(world, tmp_path, monkeypatch) -> None:
    code, rows = await run_command(tmp_path, world, world.login(), monkeypatch)
    assert code == 0, rows[-1]
    assert rows[0]["kind"] == "inventory" and rows[0]["status"] == "ok"
    assert [row["status"] for row in rows[1:-1]] == ["equal", "equal"]
    assert rows[-1]["exit_code"] == 0 and rows[-1]["inventory_status"] == "ok"


async def test_runbook_shape_passes_the_attestation(world) -> None:
    await attest(world, world.login())


async def test_missing_policy_is_reported_and_exits_nonzero(world, tmp_path, monkeypatch) -> None:
    absent = uuid4()
    argv = [*args(tmp_path, world, world.login()), "--scope", f"{absent}:{ENVIRONMENT}"]
    monkeypatch.setattr(command.time, "time_ns", lambda: 2_000_000_000)
    output = io.StringIO()
    assert await command.run(argv, output=output) == 1
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    missing = [row for row in rows if row.get("scope", {}).get("account_id") == str(absent)]
    assert len(missing) == 2
    assert all(
        row["status"] == "not_comparable" and row["reason"] == "policy_missing" and row["evidence"]
        for row in missing
    )
    assert rows[-1]["expected_scopes"] == rows[-1]["emitted_scopes"] == 4
    assert rows[-1]["exit_code"] == 1


async def test_owner_is_rejected_by_the_actual_closure_check(world, tmp_path, monkeypatch) -> None:
    owner = world.url.username
    url = world.url
    manifest = tmp_path / "owner.json"
    manifest.write_text(
        json.dumps({"mode": "cutover", "host": url.host, "port": url.port,
                    "database": url.database, "user": owner, "run_id": "integration"})
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


_COLUMN_GRANT = "GRANT UPDATE (state) ON public.trading_state TO {login}"
_UNSAFE: dict[str, tuple[str, str]] = {
    # reason: (setup SQL with {login}, a name the detail must carry; "" when none)
    "table_write_privilege": ("GRANT INSERT ON public.trading_state TO {login}", "public.trading_state"),
    "column_write_privilege": (_COLUMN_GRANT, "public.trading_state"),
    "sequence_privilege": (
        "GRANT USAGE ON SEQUENCE public.trading_state_id_seq TO {login}",
        "public.trading_state_id_seq",
    ),
    "role_closure_unexpected": (
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bfx_bot') "
        "THEN CREATE ROLE bfx_bot NOLOGIN; END IF; END $$; GRANT bfx_bot TO {login}",
        "bfx_bot",
    ),
    "role_attribute_privileged": ("ALTER ROLE {login} SUPERUSER", "{login}"),
    "security_definer_executable": (
        "CREATE FUNCTION public.guard_probe() RETURNS int LANGUAGE sql SECURITY DEFINER "
        "AS 'SELECT 1'; REVOKE ALL ON FUNCTION public.guard_probe() FROM PUBLIC; "
        "GRANT EXECUTE ON FUNCTION public.guard_probe() TO {login}",
        "public.guard_probe",
    ),
    # Owned objects sit outside the scanned schemas so only the ownership scan sees them.
    "owned_relation": (
        "CREATE SCHEMA other_schema; CREATE TABLE other_schema.owned(x int); "
        "ALTER TABLE other_schema.owned OWNER TO {login}",
        "other_schema.owned",
    ),
    "owned_function": (
        "CREATE SCHEMA other_schema; CREATE FUNCTION other_schema.owned() RETURNS int "
        "LANGUAGE sql AS 'SELECT 1'; ALTER FUNCTION other_schema.owned() OWNER TO {login}",
        "other_schema.owned",
    ),
    "owned_schema": ("CREATE SCHEMA other_schema AUTHORIZATION {login}", "other_schema"),
    "forbidden_extension": ("CREATE EXTENSION dblink", "dblink"),
}


@pytest.mark.parametrize("reason", list(_UNSAFE))
async def test_each_unsafe_shape_has_its_own_reason(world, reason) -> None:
    setup, named = _UNSAFE[reason]
    login = world.login()
    if reason == "forbidden_extension":
        with_extension = create_engine(world.url.set(drivername="postgresql+psycopg"))
        try:
            with with_extension.connect() as conn:
                available = conn.exec_driver_sql(
                    "SELECT count(*) FROM pg_available_extensions WHERE name = 'dblink'"
                ).scalar()
        finally:
            with_extension.dispose()
        if not available:
            pytest.skip("dblink is not installed in this PostgreSQL")
    world.exec(setup.format(login=login))
    with pytest.raises(GuardRejectedError) as raised:
        await attest(world, login)
    assert raised.value.reason == reason
    if named:
        assert named.format(login=login) in raised.value.detail


async def test_a_direct_login_without_membership_cannot_switch_role(world) -> None:
    with pytest.raises(GuardRejectedError) as raised:
        await attest(world, world.login(member=False))
    assert raised.value.reason == "reader_role_unavailable"


async def test_safe_neighbours_pass(world) -> None:
    """Near misses of each unsafe shape stay accepted: no false positives on the real schema."""
    login = world.login()
    world.exec(
        # SECURITY DEFINER without EXECUTE for the reachable roles; owned objects of other roles;
        # writes on a table outside the scanned schemas; SELECT on a sequence is not USAGE.
        "CREATE FUNCTION public.guard_hidden() RETURNS int LANGUAGE sql SECURITY DEFINER "
        "AS 'SELECT 1'; REVOKE ALL ON FUNCTION public.guard_hidden() FROM PUBLIC; "
        "CREATE SCHEMA other_schema; CREATE TABLE other_schema.t(x int); "
        f'GRANT INSERT, UPDATE, DELETE ON other_schema.t TO "{login}"; '
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
                f"AND conrelid = 'public.{table}'::regclass"
            ).scalars().all():
                conn.exec_driver_sql(f'ALTER TABLE public.{table} DROP CONSTRAINT "{name}"')
            columns = conn.exec_driver_sql(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                f"WHERE a.attrelid = 'public.{table}'::regclass AND a.attnum > 0 "
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
                f"INSERT INTO public.{table} ({', '.join(f'\"{n}\"' for n in names)}) "
                f"VALUES ({', '.join(values)})"
            )
    finally:
        engine.dispose()


def _clear(world: World, table: str, account: UUID) -> None:
    world.exec(
        "SET session_replication_role = replica; "
        f"DELETE FROM public.{table} WHERE exchange_account_id = '{account}'"
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

_LEGACY_READ = (
    "event_log", "event_prefix_hashes", "capital_policy_heads", "capital_policy_revisions",
    "capital_snapshots", "capital_snapshot_queries", "execution_decisions", "projection_heads",
    "submission_attempts", "execution_uncertainties",
)  # fmt: skip
_SCOPE_AND_STATE = ("exchange_account_id", "deployment_environment", "state")
_INVENTORY_READ = {
    "offer_claims": {*_SCOPE_AND_STATE, "symbol"},
    "trading_state": {"exchange_account_id", "deployment_environment"},
    "uncertainty_resolution_requests": set(_SCOPE_AND_STATE),
    "capital_policy_requests": set(_SCOPE_AND_STATE),
    "trading_control_requests": set(_SCOPE_AND_STATE),
}
_DENIED = (
    ("ledger_observation", "evidence"),
    ("ledger_observation_offer", "raw"),
    ("submission_attempt_journal", "normalized_payload"),
    ("submission_attempt_journal", "authorization_evidence"),
)


def _reader_columns(world: World) -> dict[str, set[str]]:
    engine = create_engine(world.url.set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql(
                "SELECT c.relname, a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                f"AND has_column_privilege('{READER_ROLE}', c.oid, a.attnum, 'SELECT')"
            ).all()
            tables = conn.exec_driver_sql(
                "SELECT table_name FROM information_schema.role_table_grants "
                f"WHERE grantee = '{READER_ROLE}' AND table_schema = 'public'"
            ).scalars().all()
            assert tables == [], "the group holds no table-level grant"
            writes = conn.exec_driver_sql(
                "SELECT c.relname, a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
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
    """Pins the comparison's legacy and inventory grants (migration b5c6d7e8f9a0).

    The baseline loads whole ORM rows of the legacy tables, so the reader holds every mapped
    column of them; the inventory adds only scope and state columns of its other tables.
    """
    found = _reader_columns(world)
    for table in _LEGACY_READ:
        assert found[table] == {c.name for c in Base.metadata.tables[table].columns}, table
    for table, columns in _INVENTORY_READ.items():
        assert found[table] == columns, table
    for table, column in _DENIED:
        assert column not in found.get(table, set()), (table, column)
    assert {"conservation", "lent_unexplained", "foreign_executed", "fill_conflicts"} <= (
        found["accepted_capital_basis_symbol"]
    )
    assert "intended_amount" in found["submission_attempt_journal"]
    assert "normalized_payload" not in found["submission_attempt_journal"]
