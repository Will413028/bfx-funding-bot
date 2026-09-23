from __future__ import annotations

import asyncio
import pathlib
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    ProjectionHeadRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import (
    AccountEventWriter,
    ProjectionWriteError,
)
from bfx_funding_bot.modules.execution.events import ReservationClaimed

pytestmark = pytest.mark.integration

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"
_REVISION = "f8c2d4e6a901"
_ENV = "ci"


def _reset_and_upgrade(sync_url: str) -> None:
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command

    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")


def _claimed(account_id: UUID, *, cid: int, venue_seq: int, signal: UUID | None = None) -> ReservationClaimed:
    signal_id = signal or uuid4()
    return ReservationClaimed(
        cid=cid,
        venue_offer_id=f"offer-{cid}",
        amount=Decimal("10"),
        signal_correlation_id=signal_id,
        account_id=str(account_id),
        is_simulated=True,
        venue_seq=venue_seq,
        occurred_at_ms=1_000 + venue_seq,
        symbol="fUST",
        reservation_ref=ReservationRef(
            execution_decision_id=f"decision-{cid}",
            cid=cid,
            signal_correlation_id=signal_id,
            venue_offer_id=f"offer-{cid}",
        ),
    )


async def _seed_accounts(pg_session_factory, *account_ids: UUID) -> None:
    async with pg_session_factory() as session:
        for account_id in account_ids:
            session.add(
                ExchangeAccount(id=account_id, venue="bitfinex", label=f"test-{account_id}")
            )
        await session.commit()


@pytest.mark.asyncio
async def test_serialized_projector_schema_contract(pg_engine, monkeypatch) -> None:
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)

    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(sync_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            tables = set(inspector.get_table_names(schema="public"))
            assert {"projection_heads", "venue_offer_state", "venue_credit_state"} <= tables

            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            assert revision == _REVISION

            event_columns = {
                column["name"]: column for column in inspector.get_columns("event_log")
            }
            assert event_columns["event_id"]["nullable"] is True
            assert event_columns["schema_version"]["nullable"] is False
            event_indexes = {
                index["name"]: index for index in inspector.get_indexes("event_log")
            }
            assert event_indexes["uq_event_log_event_id"]["unique"] is True

            position_columns = {
                column["name"] for column in inspector.get_columns("position_state")
            }
            assert {
                "offered_amount",
                "lent_amount",
                "available_amount",
                "uncertain_amount",
                "last_venue_snapshot_at",
            } <= position_columns
            offer_columns = {
                column["name"] for column in inspector.get_columns("venue_offer_state")
            }
            assert "offer_type" in offer_columns

            for table, identity in (
                ("venue_offer_state", {"exchange_account_id", "deployment_environment", "venue_offer_id"}),
                ("venue_credit_state", {"exchange_account_id", "deployment_environment", "credit_id"}),
            ):
                pk = set(inspector.get_pk_constraint(table)["constrained_columns"])
                assert pk == identity
                foreign_keys = inspector.get_foreign_keys(table)
                assert foreign_keys
                assert all(
                    fk["options"].get("ondelete") == "RESTRICT"
                    for fk in foreign_keys
                )
                columns = {
                    column["name"]: column for column in inspector.get_columns(table)
                }
                assert columns["flags"]["default"] is not None
                assert columns["is_terminal"]["default"] is not None

            check_constraints = inspector.get_check_constraints("event_log")
            assert any(
                constraint["name"] == "ck_event_log_v3_event_id"
                for constraint in check_constraints
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_cutover_seeds_historical_projection_cursor(
    pg_engine, pg_session_factory, monkeypatch
) -> None:
    """Historical snapshots must not be replayed a second time after cutover."""
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from alembic import command

    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
            connection.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
        config = Config(str(_ALEMBIC_INI))
        command.upgrade(config, "bc4d5e6f7081")
        account_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO exchange_accounts (id, venue, label) "
                    "VALUES (:id, 'bitfinex', 'historical')"
                ),
                {"id": account_id},
            )
            connection.execute(
                text(
                    "INSERT INTO event_log ("
                    "account_id, exchange_account_id, deployment_environment, "
                    "event_type, payload, occurred_at_ms, schema_version"
                    ") VALUES (:account_text, :account_id, 'ci', 'CREDIT_CLOSED', "
                    "'{}'::jsonb, 1000, 2)"
                ),
                {"account_text": str(account_id), "account_id": account_id},
            )
        async with pg_session_factory() as session:
            with pytest.raises(ValueError, match="cursor migration incomplete"):
                await AccountEventWriter(
                    store=PostgresEventStore(deployment_environment=_ENV)
                ).append(session, _claimed(account_id, cid=98, venue_seq=98))
            await session.rollback()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT last_event_seq, projector_version "
                    "FROM projection_heads "
                    "WHERE exchange_account_id = :account_id "
                    "AND deployment_environment = 'ci' "
                    "AND projection_name = 'execution_state'"
                ),
                {"account_id": account_id},
            ).one()
        assert row.last_event_seq == 1
        assert row.projector_version == "legacy-v2"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_same_account_transaction_lock_serializes_writers(
    pg_engine, pg_session_factory, monkeypatch
) -> None:
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)

    account_id = uuid4()
    await _seed_accounts(pg_session_factory, account_id)
    first_session = pg_session_factory()
    second_session = pg_session_factory()
    release = asyncio.Event()
    acquired = asyncio.Event()
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))

    async def first_writer() -> None:
        async with first_session as session:
            await writer.acquire_lock(session, account_id=account_id)
            acquired.set()
            await release.wait()
            await session.commit()

    async def second_writer():
        await acquired.wait()
        async with second_session as session:
            pending = asyncio.create_task(
                writer.append(session, _claimed(account_id, cid=1, venue_seq=1))
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=0.1)
            release.set()
            result = await asyncio.wait_for(pending, timeout=5)
            await session.commit()
            return result

    _, result = await asyncio.gather(first_writer(), second_writer())
    assert result.persisted is True


@pytest.mark.asyncio
async def test_cross_account_transaction_locks_do_not_block_each_other(
    pg_engine, pg_session_factory, monkeypatch
) -> None:
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)

    held_account = uuid4()
    independent_account = uuid4()
    await _seed_accounts(pg_session_factory, held_account, independent_account)
    first_session = pg_session_factory()
    second_session = pg_session_factory()
    release = asyncio.Event()
    acquired = asyncio.Event()
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))

    async def hold_first_account() -> None:
        async with first_session as session:
            await writer.acquire_lock(session, account_id=held_account)
            acquired.set()
            await release.wait()
            await session.commit()

    async def write_independent_account():
        await acquired.wait()
        async with second_session as session:
            result = await asyncio.wait_for(
                writer.append(
                    session,
                    _claimed(independent_account, cid=2, venue_seq=2),
                ),
                timeout=2,
            )
            await session.commit()
            return result

    holder = asyncio.create_task(hold_first_account())
    independent = asyncio.create_task(write_independent_account())
    result = await independent
    release.set()
    await holder
    assert result.persisted is True


@pytest.mark.asyncio
async def test_compatibility_append_fails_closed_for_unknown_migrated_account(
    pg_engine, pg_session_factory, monkeypatch
) -> None:
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)

    account_id = uuid4()
    store = PostgresEventStore(deployment_environment=_ENV)
    async with pg_session_factory() as session:
        with pytest.raises(ProjectionWriteError, match="ExchangeAccount does not exist"):
            await store.append(
                session,
                _claimed(account_id, cid=99, venue_seq=99),
            )
        await session.rollback()

    async with pg_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EventLogRow)) == 0


@pytest.mark.asyncio
async def test_writer_replays_postgres_event_log_gap_before_new_append(
    pg_engine, pg_session_factory, monkeypatch
) -> None:
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)

    account_id = uuid4()
    await _seed_accounts(pg_session_factory, account_id)
    store = PostgresEventStore(deployment_environment=_ENV)
    historical = _claimed(account_id, cid=100, venue_seq=100)
    async with pg_session_factory() as session:
        session.add(
            EventLogRow(
                account_id=str(account_id),
                exchange_account_id=account_id,
                deployment_environment=_ENV,
                event_type="RESERVATION_CLAIMED",
                cid=historical.cid,
                venue_offer_id=historical.venue_offer_id,
                venue_seq=historical.venue_seq,
                event_id=historical.event_id,
                schema_version=historical.schema_version,
                payload=serialize_event(historical),
                occurred_at_ms=historical.occurred_at_ms or 0,
            )
        )
        await session.flush()
        result = await AccountEventWriter(store=store).append(
            session,
            _claimed(account_id, cid=101, venue_seq=101),
        )
        await session.commit()

    async with pg_session_factory() as session:
        cids = (
            await session.execute(
                select(OfferClaimRow.cid)
                .where(
                    OfferClaimRow.exchange_account_id == account_id,
                    OfferClaimRow.deployment_environment == _ENV,
                )
                .order_by(OfferClaimRow.cid.asc())
            )
        ).scalars().all()
    assert result.projection_head == result.event_seq
    assert cids == [100, 101]


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_event_claim_and_head_in_postgres(
    pg_engine, pg_session_factory, monkeypatch
) -> None:
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace(
        "+asyncpg", "+psycopg"
    )
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)

    account_id = uuid4()
    await _seed_accounts(pg_session_factory, account_id)
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))
    signal = uuid4()
    async with pg_session_factory() as session:
        await writer.append(
            session,
            _claimed(account_id, cid=3, venue_seq=3, signal=signal),
        )
        with pytest.raises(ProjectionWriteError):
            await writer.append(
                session,
                _claimed(account_id, cid=3, venue_seq=4, signal=uuid4()),
            )
        await session.rollback()

    async with pg_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EventLogRow)) == 0
        assert await session.scalar(select(func.count()).select_from(OfferClaimRow)) == 0
        assert await session.scalar(select(func.count()).select_from(ProjectionHeadRow)) == 0


@pytest.mark.asyncio
async def test_archive_revision_strict_append_and_unknown_revision_rejection(
    pg_engine, pg_session_factory, monkeypatch,
) -> None:
    from sqlalchemy import text
    sync_url = pg_engine.url.render_as_string(hide_password=False).replace("+asyncpg", "+psycopg")
    monkeypatch.setenv("DATABASE_URL", sync_url)
    _reset_and_upgrade(sync_url)
    account = uuid4()
    await _seed_accounts(pg_session_factory, account)
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=_ENV))
    async with pg_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "f8c2d4e6a901"
        result = await writer.append(session, _claimed(account, cid=123, venue_seq=123))
        assert result.persisted and result.projection_head == result.event_seq
        await session.commit()
    for revision in ("unknown", "bc4d5e6f7081"):
        async with pg_session_factory() as session:
            await session.execute(text("UPDATE alembic_version SET version_num=:r"), {"r": revision})
            with pytest.raises(ValueError, match="cursor migration incomplete"):
                await writer.append(session, _claimed(account, cid=124, venue_seq=124))
            await session.rollback()
