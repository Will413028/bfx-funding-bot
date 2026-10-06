"""S1-4b table digest on a PostgreSQL ledger clone (owner-written rows, RR RO reads).

Mutations, each applied alone to ``modules/ledger/table_digest.py`` and reverted:

* order by ``ctid`` (or no ORDER BY): ``test_insert_order_does_not_change_any_digest`` fails
  (the two clones hold wallet rows in different heap order).
* drop a column / add a generated column / ``.normalize()`` on Numeric / drop ``COLLATE "C"``:
  killed by the unit file (pin, golden and SQL-shape tests); the database parity test reads the
  same canonical list, so it cannot see a wrong list by itself (it proves the two paths agree).
* skip the isolation check: ``test_digest_refuses_other_transaction_modes`` fails.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger import table_digest as td
from tests.pg_templates import disable_realm_triggers

from .test_ledger_schema_roles import (
    _A,
    _O,
    _build,
    _query_sql,
    _seed,
)

pytestmark = pytest.mark.integration
SCOPE = Scope(UUID(_A), "ci")
OTHER_SCOPE = Scope(UUID(_A), "other")
RR_RO = "ISOLATION LEVEL REPEATABLE READ READ ONLY"

_CURRENCIES = ("ETH", "usd", "BTC", "Éth")
_TRADES = (5, 3, 9, 4)


def _wallet(currency: str) -> str:
    return (
        "INSERT INTO ledger_observation_wallet(observation_id, wallet_type, currency, "
        f"available, balance) VALUES ('{_O}', 'funding', '{currency}', 1.10, 2.5)"
    )


def _trade(trade_id: int) -> str:
    return (
        "INSERT INTO ledger_observation_trade(observation_id, trade_id, symbol, "
        "venue_offer_id, amount, rate, period_days, mts_create) "
        f"VALUES ('{_O}', {trade_id}, 'fUST', 'offer-1', 1.0, 0.0001, 2, {trade_id})"
    )


def _populate(engine, order: tuple[int, ...]) -> None:
    with engine.begin() as conn:
        _seed(conn)
    with engine.begin() as conn:
        for position in order:
            conn.exec_driver_sql(_wallet(_CURRENCIES[position]))
            conn.exec_driver_sql(_trade(_TRADES[position]))


def _async_url(engine) -> str:
    return engine.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg")


async def _digest(engine, *, scope: Scope | None = None, mode: str = RR_RO) -> td.LedgerDigest:
    factory = async_sessionmaker(create_async_engine(_async_url(engine)), expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text(f"SET TRANSACTION {mode}"))
        return await td.digest_ledger(session, scope=scope)


@pytest_asyncio.fixture
async def pair(pg_templates, pg_clone) -> AsyncIterator[tuple]:
    template = pg_templates.template("ledger_s1_roles", _build)
    first = create_engine(pg_clone(template))
    second = create_engine(pg_clone(template))
    try:
        # The same facts, written in opposite orders: different heap order, same content.
        _populate(first, (0, 1, 2, 3))
        _populate(second, (3, 2, 1, 0))
        yield first, second
    finally:
        first.dispose()
        second.dispose()


@pytest.mark.asyncio
async def test_insert_order_does_not_change_any_digest(pair) -> None:
    first, second = await _digest(pair[0]), await _digest(pair[1])
    assert first == second
    assert [d.table for d in first.tables] == list(td.DIGEST_TABLES)
    assert first.table("ledger_observation_wallet").count == 5
    assert first.table("ledger_observation_trade").count == 5
    assert all(len(d.sha256) == 64 for d in first.tables)


@pytest.mark.asyncio
async def test_database_digest_equals_the_in_memory_digest(pair) -> None:
    """Pure and database paths share one canonicalizer: same rows, same bytes, per table."""
    engine = pair[0]
    database = await _digest(engine)
    for name in td.DIGEST_TABLES:
        table = td._TABLES[name]
        columns = td.CANONICAL_COLUMNS[name]
        with engine.connect() as conn:
            rows = [
                dict(row._mapping)
                for row in conn.execute(select(*(table.c[column] for column in columns)))
            ]
        pure = td.digest_rows(name, rows)
        stored = database.table(name)
        assert (pure.count, pure.sha256, pure.watermark) == (
            stored.count,
            stored.sha256,
            stored.watermark,
        ), name
        assert stored.count >= 1, name  # the seed fills every table, so none is vacuous


@pytest.mark.asyncio
async def test_watermarks_and_mutable_marks(pair) -> None:
    digest = await _digest(pair[0])
    marks = {d.table: d.watermark for d in digest.tables if d.watermark is not None}
    with pair[0].connect() as conn:
        expected = {
            "capital_command_clock": conn.scalar(text("SELECT max(revision) FROM capital_command_clock")),
            "ledger_observation_query": conn.scalar(
                text("SELECT max(query_revision) FROM ledger_observation_query")
            ),
            "submission_attempt_journal": conn.scalar(
                text("SELECT max(attempt_seq) FROM submission_attempt_journal")
            ),
            "quarantine_opening": conn.scalar(text("SELECT max(opened_revision) FROM quarantine_opening")),
            "capital_authority_epoch": conn.scalar(text("SELECT max(epoch_seq) FROM capital_authority_epoch")),
        }
    assert marks == expected
    assert marks == {
        "capital_command_clock": 0,
        "ledger_observation_query": 1,
        "submission_attempt_journal": 1,
        "quarantine_opening": 1,
        "capital_authority_epoch": 2,  # the initial legacy row and the genesis ledger row
    }
    assert {d.table for d in digest.tables if d.mutable} == td.MUTABLE_TABLES


@pytest.mark.asyncio
async def test_watermarks_follow_new_rows(pair) -> None:
    engine = pair[0]
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            "SELECT max(epoch_seq) + 1, 'ledger', 2, 'test', 'watermark' FROM capital_authority_epoch"
        )
        conn.exec_driver_sql("UPDATE capital_command_clock SET revision = 4")
    before = await _digest(pair[1])
    after = await _digest(engine)
    assert after.table("capital_authority_epoch").watermark == (
        before.table("capital_authority_epoch").watermark + 1)
    assert after.table("capital_authority_epoch").count == before.table("capital_authority_epoch").count + 1
    assert after.table("capital_command_clock").watermark == 4
    assert after.table("capital_command_clock").sha256 != before.table("capital_command_clock").sha256


@pytest.mark.asyncio
async def test_scope_filter_separates_scopes_and_joins_children(pair) -> None:
    engine = pair[0]
    disable_realm_triggers(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_command_clock(exchange_account_id, deployment_environment, revision) "
            f"VALUES ('{_A}', 'other', 7)"
        )
        conn.exec_driver_sql(_query_sql("00000000-0000-0000-0000-00000000b001", 3, environment="other"))
    whole = await _digest(engine)
    ci = await _digest(engine, scope=SCOPE)
    other = await _digest(engine, scope=OTHER_SCOPE)
    assert whole.scope is None and ci.scope == SCOPE
    assert whole.table("capital_command_clock").count == 2
    assert ci.table("capital_command_clock").count == 1
    assert other.table("capital_command_clock").count == 1
    assert other.table("capital_command_clock").watermark == 7
    assert other.table("ledger_observation_query").watermark == 3
    assert ci.table("ledger_observation_query").watermark == 1
    assert whole.table("ledger_observation_query").watermark == 3
    # Child tables reach their scope through their parent: the "other" scope owns none.
    for name in td.DIGEST_TABLES:
        if name in ("capital_authority_epoch", "capital_command_clock", "ledger_observation_query"):
            continue
        assert other.table(name).count == 0, name
        assert ci.table(name).count == whole.table(name).count, name
        assert ci.table(name).sha256 == whole.table(name).sha256, name
    # The epoch is global.
    assert other.table("capital_authority_epoch") == whole.table("capital_authority_epoch")
    assert other.table("capital_authority_epoch").scope is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "ISOLATION LEVEL READ COMMITTED READ ONLY",
        "ISOLATION LEVEL SERIALIZABLE READ ONLY",
        "ISOLATION LEVEL REPEATABLE READ READ WRITE",
        "ISOLATION LEVEL READ COMMITTED READ WRITE",
    ],
)
async def test_digest_refuses_other_transaction_modes(pair, mode) -> None:
    with pytest.raises(td.TableDigestRefused):
        await _digest(pair[0], mode=mode)


@pytest.mark.asyncio
async def test_digest_does_not_write(pair) -> None:
    before = await _digest(pair[0])
    again = await _digest(pair[0])
    assert before == again
    with pair[0].connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM ledger_observation_wallet")) == 5
