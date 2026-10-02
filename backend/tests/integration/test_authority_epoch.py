"""The capital authority epoch and the dormant-ledger write guard on clone PostgreSQL.

Mutation checks (one at a time; revert after each):

* Drop the epoch condition from ``guard_ledger_authority``. The bot's attempt
  INSERT under ``legacy`` succeeds.
* Treat a missing epoch row as legacy in ``read_authority``. The empty-table read
  returns instead of refusing.
* Accept an unknown authority value in ``check_authority``. The unknown-value read
  returns instead of refusing.
* Read the first instead of the latest epoch row. The switched database reads
  ``legacy`` and the ``ledger`` read no longer refuses.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bfx_funding_bot.core.authority import AuthorityMismatch, read_authority
from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES
from tests.pg_templates import alembic

from .test_ledger_schema_roles import _A, _B, _D2, _P, _build, _query_sql, _seed

pytestmark = pytest.mark.integration

_REVISION = "f6a7b8c9d0e1"
_PREVIOUS = "e5f6a7b8c9d0"
_ATTEMPT = f"""INSERT INTO submission_attempt_journal(attempt_id, execution_decision_id,
  exchange_account_id, deployment_environment, symbol, cell_id, attempt_seq,
  normalized_payload, payload_sha256, basis_id, policy_revision_id,
  authorization_evidence, started_at_ms)
  VALUES ('{{attempt}}', '{_D2}', '{_A}', 'ci', 'fUST', 'cell', 2, '{{{{"amount": "1"}}}}', 'hash',
  '{_B}', '{_P}', '{{{{}}}}', 4)"""


@pytest.fixture
def ledger_db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("ledger_s1_roles", _build))
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def seeded(ledger_db):
    with ledger_db.begin() as conn:
        _seed(conn)
    return ledger_db


def _append(conn, seq: int, authority: str) -> None:
    conn.exec_driver_sql(
        "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
        f"VALUES ({seq}, '{authority}', {seq}, 'test', 'switch')"
    )


def _read(engine: Engine) -> str:
    url = engine.url.render_as_string(hide_password=False)

    async def read() -> str:
        async_engine = create_async_engine(url)
        try:
            async with async_engine.connect() as conn, AsyncSession(bind=conn) as session:
                return await read_authority(session)
        finally:
            await async_engine.dispose()

    return asyncio.run(read())


def test_seed_is_one_legacy_row(ledger_db) -> None:
    with ledger_db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT epoch_seq, authority, actor, reason, evidence, set_at_ms > 0 "
                "FROM capital_authority_epoch"
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        (1, "legacy", "migration f6a7b8c9d0e1", "initial authority", None, True)
    ]
    assert _read(ledger_db) == "legacy"


def test_epoch_rows_are_immutable(ledger_db) -> None:
    for statement in (
        "UPDATE capital_authority_epoch SET authority='ledger'",
        "DELETE FROM capital_authority_epoch",
        "TRUNCATE capital_authority_epoch",
    ):
        with ledger_db.begin() as conn, pytest.raises(Exception, match="immutable ledger evidence"):
            conn.exec_driver_sql(statement)


def test_only_the_owner_appends_an_epoch(ledger_db) -> None:
    with ledger_db.connect() as conn:
        for role in ("bfx_bot", "bfx_webapi", "bfx_cutover_reader"):
            assert conn.scalar(
                text("SELECT has_any_column_privilege(:r,'capital_authority_epoch','SELECT')"),
                {"r": role},
            )
        for role in ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader"):
            for privilege in ("INSERT", "UPDATE"):
                assert not conn.scalar(
                    text("SELECT has_any_column_privilege(:r,'capital_authority_epoch',:p)"),
                    {"r": role, "p": privilege},
                )
            for privilege in ("DELETE", "TRUNCATE"):
                assert not conn.scalar(
                    text("SELECT has_table_privilege(:r,'capital_authority_epoch',:p)"),
                    {"r": role, "p": privilege},
                )
        assert not conn.scalar(
            text(
                "SELECT has_any_column_privilege('bfx_webauth','capital_authority_epoch','SELECT')"
            )
        )
        # The reader group keeps column grants only.
        assert not conn.scalar(
            text(
                "SELECT has_table_privilege('bfx_cutover_reader','capital_authority_epoch','SELECT')"
            )
        )
    for role in ("bfx_bot", "bfx_webapi"):
        with ledger_db.begin() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            _append(conn, 2, "ledger")
        with ledger_db.begin() as conn:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            assert conn.scalar(text("SELECT authority FROM capital_authority_epoch")) == "legacy"


def test_guard_is_on_every_ledger_fact_table(ledger_db) -> None:
    with ledger_db.connect() as conn:
        guarded = set(
            conn.scalars(
                text(
                    "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE t.tgname = 'ledger_authority_write'"
                )
            )
        )
        assert (
            conn.scalar(
                text(
                    "SELECT has_function_privilege('bfx_cutover_reader',"
                    "'public.guard_ledger_authority()','EXECUTE')"
                )
            )
            is False
        )
    assert guarded == {table.name for table in LEDGER_TABLES}


def test_bot_writes_are_rejected_until_the_ledger_epoch(seeded) -> None:
    # The owner seeded every ledger table under legacy (``seeded``); the bot may not.
    with seeded.begin() as conn, pytest.raises(Exception, match="requires ledger authority"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(_ATTEMPT.format(attempt=uuid4()))
    for statement in (
        "UPDATE capital_command_clock SET revision=1",
        "UPDATE venue_offer_mirror SET amount_remaining=2",
        "UPDATE venue_credit_mirror SET amount=2",
    ):
        with seeded.begin() as conn, pytest.raises(Exception, match="requires ledger authority"):
            conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
            conn.exec_driver_sql(statement)
    with seeded.begin() as conn:  # the owner still writes under legacy
        conn.exec_driver_sql(_query_sql(str(uuid4()), 2, environment="owner"))
    with seeded.begin() as conn:
        _append(conn, 2, "ledger")
    with seeded.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(_ATTEMPT.format(attempt=uuid4()))
        conn.exec_driver_sql("UPDATE capital_command_clock SET revision=1")
    # Latest wins: a later legacy epoch holds the ledger dormant again.
    with seeded.begin() as conn:
        _append(conn, 3, "legacy")
    with seeded.begin() as conn, pytest.raises(Exception, match="requires ledger authority"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE capital_command_clock SET revision=2")


def test_read_authority_takes_the_latest_row(ledger_db) -> None:
    with ledger_db.begin() as conn:
        _append(conn, 2, "ledger")
    with pytest.raises(AuthorityMismatch, match="authority_unsupported value=ledger"):
        _read(ledger_db)
    with ledger_db.begin() as conn:
        _append(conn, 3, "legacy")
    assert _read(ledger_db) == "legacy"


def test_read_authority_refuses_an_unknown_value(ledger_db) -> None:
    with ledger_db.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE capital_authority_epoch DROP CONSTRAINT ck_capital_authority_epoch_authority"
        )
        _append(conn, 2, "bogus")
    with pytest.raises(AuthorityMismatch, match="authority_unknown value='bogus'"):
        _read(ledger_db)


def test_read_authority_refuses_a_missing_row_or_table(ledger_db) -> None:
    with ledger_db.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE capital_authority_epoch DISABLE TRIGGER USER")
        conn.exec_driver_sql("DELETE FROM capital_authority_epoch")
    with pytest.raises(AuthorityMismatch, match="authority_missing row"):
        _read(ledger_db)
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    with ledger_db.begin() as conn:
        _append(conn, 1, "legacy")
    alembic(url, "downgrade", _PREVIOUS)
    with pytest.raises(AuthorityMismatch, match="authority_missing table"):
        _read(ledger_db)


def _guard_state(conn) -> tuple[object, ...]:
    return (
        sorted(conn.scalars(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))),
        sorted(
            conn.scalars(
                text("SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace")
            )
        ),
        sorted(
            conn.execute(
                text(
                    "SELECT grantee, table_name, privilege_type "
                    "FROM information_schema.role_table_grants WHERE table_schema='public'"
                )
            ).all()
        ),
        sorted(
            conn.execute(
                text(
                    "SELECT grantee, table_name, column_name, privilege_type "
                    "FROM information_schema.role_column_grants WHERE table_schema='public'"
                )
            ).all()
        ),
    )


def test_downgrade_round_trip_restores_the_prior_state(ledger_db) -> None:
    url = ledger_db.url.render_as_string(hide_password=False)
    with ledger_db.connect() as conn:
        at_head = _guard_state(conn)
    ledger_db.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    with ledger_db.connect() as conn:
        before = _guard_state(conn)
        assert "capital_authority_epoch" not in inspect(conn).get_table_names()
        assert "guard_ledger_authority" not in before[1]
        assert "ledger_authority_write" not in before[0]
        conn.rollback()
    ledger_db.dispose()
    # Without the guard the prior build's bot writes as before.
    with ledger_db.begin() as conn:
        _seed(conn)
    with ledger_db.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(_ATTEMPT.format(attempt=uuid4()))
    ledger_db.dispose()
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    with ledger_db.connect() as conn:
        assert _guard_state(conn) == at_head
    assert _read(ledger_db) == "legacy"


def test_switched_authority_refuses_downgrade(ledger_db) -> None:
    with ledger_db.begin() as conn:
        _append(conn, 2, "ledger")
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    with pytest.raises(Exception, match="refuse downgrade of switched authority"):
        alembic(url, "downgrade", _PREVIOUS)
