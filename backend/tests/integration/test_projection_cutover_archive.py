"""Disposable PostgreSQL 18 only; archive DDL is applied through Alembic."""

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from bfx_funding_bot.modules.execution.projection_cutover.archive import (
    audit_runtime_roles,
    capture_archive,
    verify_archive,
)
from bfx_funding_bot.modules.execution.projection_cutover.codec import decode_row
from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope

from .test_ledger_schema_roles import pre_switch

pytestmark = pytest.mark.integration
ACCOUNT = UUID(int=100)
SCOPE = Scope(ACCOUNT, "ci")
INI = Path(__file__).resolve().parents[2] / "alembic.ini"


async def capture(factory):
    async with factory.begin() as session:
        return await capture_archive(
            session,
            scope=SCOPE,
            run_id=uuid4(),
            image_digest="sha256:synthetic",
            projector_version="execution-state-v1",
        )


async def test_exact_rows_empty_schemas_scope_and_independent_manifest(archive_db):
    factory, _ = archive_db
    expected = await capture(factory)
    assert expected.stream.count == expected.stream.head == 0
    assert expected.stream.digest == hashlib.sha256(b"[]").hexdigest()
    assert len(expected.tables) == 8
    async with factory() as session:
        await verify_archive(session, expected=expected)
        for entry in expected.tables:
            name = entry["name"]
            assert set(entry) == {"name", "schema", "key_columns", "count", "digest"}
            archived = (
                (
                    await session.execute(
                        text(
                            "SELECT encoded_payload FROM projection_audit.rows WHERE run_id=:id AND table_name=:name ORDER BY row_key"
                        ),
                        {"id": expected.run_id, "name": name},
                    )
                )
                .scalars()
                .all()
            )
            live = (
                (
                    await session.execute(
                        text(
                            f"SELECT * FROM public.{name} WHERE exchange_account_id=:id AND deployment_environment='ci'"
                        ),
                        {"id": ACCOUNT},
                    )
                )
                .mappings()
                .all()
            )
            assert [decode_row(p) for p in archived] == [dict(row) for row in live]
            assert entry["count"] == len(live)
            assert (
                {c["name"] for c in entry["schema"]} == set(live[0])
                if live
                else bool(entry["schema"])
            )
        with pytest.raises(ValueError):
            await verify_archive(session, expected=replace(expected, image_digest="tampered"))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE projection_audit.runs SET complete=false WHERE run_id=:id",
        "DELETE FROM projection_audit.runs WHERE run_id=:id",
        "UPDATE projection_audit.rows SET row_digest='changed' WHERE run_id=:id",
        "DELETE FROM projection_audit.rows WHERE run_id=:id",
        "INSERT INTO projection_audit.rows SELECT run_id,table_name,row_key || 'x'::bytea,encoded_payload,row_digest FROM projection_audit.rows WHERE run_id=:id",
        "TRUNCATE projection_audit.rows",
        "TRUNCATE projection_audit.runs CASCADE",
    ],
)
async def test_completed_archive_rejects_changes(archive_db, sql):
    factory, _ = archive_db
    expected = await capture(factory)
    from sqlalchemy.exc import DBAPIError

    async with factory.begin() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text(sql), {"id": expected.run_id})


async def test_duplicate_run_and_caller_rollback(archive_db):
    factory, _ = archive_db
    expected = await capture(factory)
    async with factory() as session:
        with pytest.raises(ValueError, match="exists"):
            await capture_archive(
                session,
                scope=SCOPE,
                run_id=expected.run_id,
                image_digest="same",
                projector_version="same",
            )
    async with factory() as session:
        rolled_back = await capture_archive(
            session, scope=SCOPE, run_id=uuid4(), image_digest="same", projector_version="same"
        )
        await session.rollback()
    async with factory() as session:
        with pytest.raises(ValueError):
            await verify_archive(session, expected=rolled_back)


@pytest.mark.parametrize(
    "damage",
    ["missing", "extra", "payload", "key", "schema", "manifest", "coherent", "extra_same_table"],
)
async def test_verifier_detects_privileged_tampering(archive_db, damage):
    factory, engine = archive_db
    expected = await capture(factory)
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        if damage == "missing":
            conn.execute(
                text(
                    "DELETE FROM projection_audit.rows WHERE run_id=:id AND table_name='position_state'"
                ),
                {"id": expected.run_id},
            )
        elif damage in {"payload", "key"}:
            field = "encoded_payload" if damage == "payload" else "row_key"
            conn.execute(
                text(f"UPDATE projection_audit.rows SET {field}='broken'::bytea WHERE run_id=:id"),
                {"id": expected.run_id},
            )
        elif damage == "extra":
            conn.execute(
                text(
                    "INSERT INTO projection_audit.rows SELECT run_id,'unexpected',row_key,encoded_payload,row_digest FROM projection_audit.rows WHERE run_id=:id"
                ),
                {"id": expected.run_id},
            )
        elif damage in {"coherent", "extra_same_table"}:
            from decimal import Decimal

            from bfx_funding_bot.modules.execution.projection_cutover.codec import encode_row

            row = conn.execute(
                text(
                    "SELECT encoded_payload FROM projection_audit.rows WHERE run_id=:id AND table_name='position_state'"
                ),
                {"id": expected.run_id},
            ).scalar_one()
            values = decode_row(row)
            values["reserved"] = Decimal("444.00")
            if damage == "extra_same_table":
                values["symbol"] = "fEUR"
            payload = encode_row(values)
            digest = hashlib.sha256(payload).hexdigest()
            if damage == "coherent":
                conn.execute(
                    text(
                        "UPDATE projection_audit.rows SET encoded_payload=:p,row_digest=:d WHERE run_id=:id AND table_name='position_state'"
                    ),
                    {"p": payload, "d": digest, "id": expected.run_id},
                )
            else:
                key = encode_row(
                    {
                        k: values[k]
                        for k in ("exchange_account_id", "deployment_environment", "symbol")
                    }
                )
                conn.execute(
                    text(
                        "INSERT INTO projection_audit.rows VALUES (:id,'position_state',:k,:p,:d)"
                    ),
                    {"id": expected.run_id, "k": key, "p": payload, "d": digest},
                )
        elif damage == "schema":
            conn.exec_driver_sql("ALTER TABLE venue_credit_state ADD COLUMN surprise text")
        else:
            conn.execute(
                text("UPDATE projection_audit.runs SET manifest='broken'::bytea WHERE run_id=:id"),
                {"id": expected.run_id},
            )
    async with factory() as session:
        with pytest.raises(ValueError):
            await verify_archive(session, expected=expected)


async def test_runtime_role_denial_superuser_failclosed_and_select_only_verifier(archive_db):
    factory, engine = archive_db
    expected = await capture(factory)
    with engine.begin() as conn:
        for role in ("archive_bot", "archive_webapi", "archive_verifier"):
            if not conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": role}
            ).scalar():
                conn.exec_driver_sql(
                    f"CREATE ROLE {role} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                )
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA projection_audit,public TO archive_verifier")
        conn.exec_driver_sql(
            "GRANT SELECT ON ALL TABLES IN SCHEMA projection_audit,public TO archive_verifier"
        )
    from sqlalchemy.exc import DBAPIError

    async with factory() as session:
        await audit_runtime_roles(session, role_names=("archive_bot", "archive_webapi"))
        owner = await session.scalar(text("SELECT current_user"))
        with pytest.raises(ValueError):
            await audit_runtime_roles(session, role_names=(owner,))
        with pytest.raises(ValueError):
            await audit_runtime_roles(session, role_names=("nonexistent",))
    for role in ("archive_bot", "archive_webapi", "archive_verifier"):
        async with factory() as session:
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            with pytest.raises(DBAPIError):
                await session.execute(text("DELETE FROM projection_audit.rows"))
    async with factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL ROLE archive_verifier"))
        await verify_archive(session, expected=expected)


async def test_populated_downgrade_refused(archive_db):
    factory, engine = archive_db
    await capture(factory)
    with engine.begin() as conn:
        pre_switch(conn)  # a switched database refuses a downgrade through f6a7b8c9d0e1
    with pytest.raises(Exception, match="populated"):
        command.downgrade(Config(str(INI)), "e7b1c2d3e4f5")


async def test_role_audit_refuses_missing_archive_table(archive_db):
    factory, engine = archive_db
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE ROLE incomplete_audit_runtime NOSUPERUSER")
        conn.exec_driver_sql("DROP TABLE projection_audit.receipts")
    async with factory() as session:
        with pytest.raises(ValueError):
            await audit_runtime_roles(session, role_names=("incomplete_audit_runtime",))


async def test_receipt_is_separately_insert_only_and_requires_completed_run(archive_db):
    from sqlalchemy.exc import DBAPIError

    from bfx_funding_bot.modules.execution.projection_cutover.codec import encode_row

    factory, _ = archive_db
    expected = await capture(factory)
    payload = encode_row({"synthetic": True})
    statement = text(
        "INSERT INTO projection_audit.receipts(run_id,snapshot_identity,new_stream,active_hashes,evidence) VALUES (:id,:p,:p,:p,:p)"
    )
    async with factory.begin() as session:
        await session.execute(statement, {"id": expected.run_id, "p": payload})
    for sql in (
        "UPDATE projection_audit.receipts SET evidence='changed'::bytea",
        "DELETE FROM projection_audit.receipts",
        "TRUNCATE projection_audit.receipts",
    ):
        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(text(sql))
    for identity in (expected.run_id, uuid4()):
        async with factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(statement, {"id": identity, "p": payload})
    async with factory() as session:
        await verify_archive(session, expected=expected)
    async with factory() as session:
        run_id = uuid4()
        await session.execute(
            text("INSERT INTO projection_audit.runs VALUES (:id,:a,'ci',''::bytea,false)"),
            {"id": run_id, "a": ACCOUNT},
        )
        with pytest.raises(DBAPIError):
            await session.execute(statement, {"id": run_id, "p": payload})


async def test_incomplete_archive_cannot_commit(archive_db):
    from sqlalchemy.exc import DBAPIError

    factory, _ = archive_db
    async with factory() as session:
        await session.execute(
            text("INSERT INTO projection_audit.runs VALUES (:id,:a,'ci',''::bytea,false)"),
            {"id": uuid4(), "a": ACCOUNT},
        )
        with pytest.raises(DBAPIError, match="must complete"):
            await session.commit()


async def test_archive_check_detects_named_schema_drift(archive_db):
    _, engine = archive_db
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE projection_audit.receipts ADD COLUMN surprise text")
    with pytest.raises(Exception, match="surprise"):
        command.check(Config(str(INI)))


async def test_role_audit_detects_column_public_and_set_role_paths(archive_db):
    factory, engine = archive_db
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE ROLE archive_column_writer NOSUPERUSER NOINHERIT")
        conn.exec_driver_sql("CREATE ROLE archive_member NOSUPERUSER NOINHERIT")
        conn.exec_driver_sql("GRANT archive_column_writer TO archive_member")
        conn.exec_driver_sql(
            "GRANT UPDATE(manifest) ON projection_audit.runs TO archive_column_writer"
        )
    async with factory() as session:
        for role in ("archive_column_writer", "archive_member"):
            with pytest.raises(ValueError):
                await audit_runtime_roles(session, role_names=(role,))
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "REVOKE UPDATE(manifest) ON projection_audit.runs FROM archive_column_writer"
        )
        conn.exec_driver_sql("GRANT INSERT ON projection_audit.rows TO PUBLIC")
    async with factory() as session:
        with pytest.raises(ValueError):
            await audit_runtime_roles(session, role_names=("archive_member",))


async def test_capture_waits_for_writer_and_hashes_fresh_canonical_events(archive_db):
    import asyncio

    from sqlalchemy import select

    from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
    from tests.integration.test_serialized_projector_pg import _claimed

    factory, _ = archive_db
    async with factory() as session:
        writer = AccountEventWriter(store=PostgresEventStore(deployment_environment="ci"))
        await writer.acquire_lock(session, account_id=ACCOUNT)
        task = asyncio.create_task(capture(factory))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.1)
            await writer.append(session, _claimed(ACCOUNT, cid=45, venue_seq=45))
            await session.commit()
            expected = await asyncio.wait_for(task, 10)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with factory() as session:
        rows = (await session.scalars(select(EventLogRow).order_by(EventLogRow.event_seq))).all()
        assert expected.stream.count == 1
        assert expected.stream.head == rows[-1].event_seq
        assert expected.stream.digest == canonical_event_hash(rows)
    async with factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        with pytest.raises(ValueError, match="READ COMMITTED"):
            await capture_archive(
                session,
                scope=SCOPE,
                run_id=uuid4(),
                image_digest="synthetic",
                projector_version="execution-state-v1",
            )


async def test_large_capture_and_verify_bound_memory_and_order_encoded_keys(archive_db):
    import tracemalloc

    from bfx_funding_bot.modules.execution.projection_cutover.codec import encode_row

    factory, engine = archive_db
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO venue_offer_state(exchange_account_id,deployment_environment,venue_offer_id,symbol,
              amount_original,amount_remaining,status,flags,mts_created,mts_updated,first_seen_event_seq,last_seen_event_seq)
            SELECT :a,'ci',i::text,'fUST',1.2300,0.1200,'ACTIVE',
              jsonb_build_object('text',repeat('x',8192),'number',1.234567890123456789::numeric),1,2,3,4
            FROM generate_series(1,2000) i
        """),
            {"a": ACCOUNT},
        )
    tracemalloc.start()
    try:
        expected = await capture(factory)
        async with factory() as session:
            await verify_archive(session, expected=expected)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Source payload alone is >16MiB; a full materialization fails this bound.
    assert peak < 14 * 1024 * 1024
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT row_key,encoded_payload FROM projection_audit.rows WHERE run_id=:id AND table_name='venue_offer_state' ORDER BY row_key"
            ),
            {"id": expected.run_id},
        ).all()
    assert len(rows) == 2000
    assert [key for key, _ in rows] == sorted(key for key, _ in rows)
    decoded = decode_row(rows[0][1])
    from decimal import Decimal

    assert decoded["flags"]["number"] == Decimal("1.234567890123456789")
    assert encode_row(decode_row(rows[0][0])) == rows[0][0]
    digest = hashlib.sha256()
    for _, payload in rows:
        digest.update(len(payload).to_bytes(8, "big") + payload)
    entry = next(t for t in expected.tables if t["name"] == "venue_offer_state")
    assert entry["digest"] == digest.hexdigest()


async def test_all_eight_tables_preserve_every_column_after_live_rows_change(archive_db):
    from datetime import UTC, datetime
    from decimal import Decimal

    from sqlalchemy import JSON, BigInteger, Integer, MetaData, Numeric, Table

    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
    from bfx_funding_bot.modules.execution.projection_cutover.codec import encode_row
    from tests.integration.test_serialized_projector_pg import _claimed

    names = (
        "execution_uncertainties",
        "offer_claims",
        "position_state",
        "projection_heads",
        "reconcile_observation",
        "submission_attempts",
        "venue_credit_state",
        "venue_offer_state",
    )
    factory, engine = archive_db
    async with factory.begin() as session:
        writer = AccountEventWriter(store=PostgresEventStore(deployment_environment="ci"))
        await writer.append(session, _claimed(ACCOUNT, cid=456, venue_seq=456))
    with engine.begin() as conn:
        decision = Table("execution_decisions", MetaData(), autoload_with=conn)
        values = {}
        for column in decision.c:
            if not column.nullable:
                values[column.name] = (
                    {}
                    if isinstance(column.type, JSON)
                    else Decimal("1.2300")
                    if isinstance(column.type, Numeric)
                    else 1
                    if isinstance(column.type, (Integer, BigInteger))
                    else "synthetic"
                )
        values.update(
            decision_id="decision-fixture",
            account_id=str(ACCOUNT),
            exchange_account_id=ACCOUNT,
            deployment_environment="ci",
            symbol="fUST",
        )
        conn.execute(decision.insert(), values)
        conn.execute(
            text("""
            INSERT INTO submission_attempts(execution_decision_id,exchange_account_id,deployment_environment,
              symbol,cid,normalized_payload,payload_sha256,started_at_ms,recorded_at)
            VALUES ('decision-fixture',:a,'ci','fUST',8,'{"nested":[null,true,7]}','synthetic',1,:t)
        """),
            {"a": ACCOUNT, "t": datetime(2002, 3, 4, tzinfo=UTC)},
        )
        conn.execute(
            text("""
            INSERT INTO execution_uncertainties(exchange_account_id,deployment_environment,symbol,kind,
              correlation_key,intended_amount,evidence,opened_event_seq)
            VALUES (:a,'ci','fUST','submit_outcome_unknown','synthetic',1.2300,'{"n": null}',1)
        """),
            {"a": ACCOUNT},
        )
        conn.execute(
            text("""
            INSERT INTO venue_credit_state(exchange_account_id,deployment_environment,credit_id,symbol,
              amount,status,flags,first_seen_event_seq,last_seen_event_seq,period)
            VALUES (:a,'ci','credit','fUST',2.3400,'ACTIVE','{"nested":[true,null,7]}',1,1,2)
        """),
            {"a": ACCOUNT},
        )
        conn.execute(
            text("""
            INSERT INTO venue_offer_state(exchange_account_id,deployment_environment,venue_offer_id,symbol,
              amount_original,amount_remaining,status,flags,mts_created,mts_updated,first_seen_event_seq,last_seen_event_seq,period)
            VALUES (:a,'ci','offer','fUST',2.3400,1.200,'ACTIVE','{}',1,1,1,1,7)
        """),
            {"a": ACCOUNT},
        )
        originals = {
            name: [
                dict(row)
                for row in conn.execute(
                    text(
                        f"SELECT * FROM public.{name} WHERE exchange_account_id=:a AND deployment_environment='ci'"
                    ),
                    {"a": ACCOUNT},
                ).mappings()
            ]
            for name in names
        }
        # Other account, same environment: neither row may enter this archive.
        conn.execute(
            text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:a,'bitfinex','other')"),
            {"a": UUID(int=101)},
        )
        conn.execute(
            text(
                "INSERT INTO position_state(account_id,exchange_account_id,deployment_environment,symbol,last_updated_ms) VALUES (:s,:a,'ci','fEUR',888)"
            ),
            {"a": UUID(int=101), "s": str(UUID(int=101))},
        )
    expected = await capture(factory)
    async with factory() as session:
        for name in names:
            payloads = (
                (
                    await session.execute(
                        text(
                            "SELECT encoded_payload FROM projection_audit.rows WHERE run_id=:id AND table_name=:n"
                        ),
                        {"id": expected.run_id, "n": name},
                    )
                )
                .scalars()
                .all()
            )
            assert payloads and sorted(payloads) == sorted(encode_row(r) for r in originals[name])
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE position_state SET reserved=999")
        conn.exec_driver_sql("DELETE FROM reconcile_observation")
    async with factory() as session:
        await verify_archive(session, expected=expected)


async def test_capture_as_non_superuser_operator(archive_db):
    factory, engine = archive_db
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE ROLE archive_capture_operator NOSUPERUSER NOCREATEROLE NOCREATEDB"
        )
        conn.exec_driver_sql(
            "GRANT USAGE ON SCHEMA public,projection_audit TO archive_capture_operator"
        )
        conn.exec_driver_sql(
            "GRANT SELECT ON ALL TABLES IN SCHEMA public,projection_audit TO archive_capture_operator"
        )
        conn.exec_driver_sql(
            "GRANT INSERT,UPDATE ON projection_audit.runs TO archive_capture_operator"
        )
        conn.exec_driver_sql("GRANT INSERT ON projection_audit.rows TO archive_capture_operator")
    async with factory.begin() as session:
        await session.execute(text("SET LOCAL ROLE archive_capture_operator"))
        expected = await capture_archive(
            session,
            scope=SCOPE,
            run_id=uuid4(),
            image_digest="synthetic",
            projector_version="execution-state-v1",
        )
    async with factory() as session:
        await verify_archive(session, expected=expected)


async def test_unknown_table_corruption_cannot_force_unbounded_verifier_memory(archive_db):
    import tracemalloc

    factory, engine = archive_db
    expected = await capture(factory)
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        conn.execute(
            text("""
            INSERT INTO projection_audit.rows
            SELECT :id, i::text || repeat('x',1024), ''::bytea, ''::bytea, 'invalid'
            FROM generate_series(1,15000) i
        """),
            {"id": expected.run_id},
        )
    tracemalloc.start()
    try:
        async with factory() as session:
            with pytest.raises(ValueError):
                await verify_archive(session, expected=expected)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


async def test_capture_rejects_write_time_payload_and_digest_replacement(
    archive_db, tmp_path, monkeypatch
):
    from decimal import Decimal
    from functools import partial
    from tempfile import TemporaryDirectory

    from bfx_funding_bot.modules.execution.projection_cutover import archive
    from bfx_funding_bot.modules.execution.projection_cutover.codec import encode_row

    monkeypatch.setattr(archive, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path))
    factory, engine = archive_db
    with engine.begin() as conn:
        source = dict(
            conn.execute(text("SELECT * FROM position_state WHERE deployment_environment='ci'"))
            .mappings()
            .one()
        )
        source["reserved"] = Decimal("555.000")
        payload = encode_row(source)
        digest = hashlib.sha256(payload).hexdigest()
        conn.exec_driver_sql(f"""
            CREATE FUNCTION public.corrupt_archive_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.table_name = 'position_state' THEN
                NEW.encoded_payload = decode('{payload.hex()}', 'hex');
                NEW.row_digest = '{digest}';
              END IF;
              RETURN NEW;
            END $$
        """)
        conn.exec_driver_sql(
            "CREATE TRIGGER zz_corrupt_archive_insert BEFORE INSERT ON projection_audit.rows FOR EACH ROW EXECUTE FUNCTION public.corrupt_archive_insert()"
        )
    async with factory() as session:
        with pytest.raises(ValueError, match=r"source.*(content|digest)"):
            await capture_archive(
                session,
                scope=SCOPE,
                run_id=uuid4(),
                image_digest="synthetic",
                projector_version="execution-state-v1",
            )
        await session.rollback()
    assert list(tmp_path.iterdir()) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM projection_audit.runs")).scalar_one() == 0
        assert conn.execute(
            text("SELECT reserved FROM position_state WHERE deployment_environment='ci'")
        ).scalar_one() == Decimal("1.2300")


@pytest.mark.parametrize("iteration", [0, 1])
async def test_shared_pg_fixture_resets_previous_audit_schema(pg_engine, iteration):
    # Each parameter is a separate test using the SAME session testcontainer.
    # Leave a populated audit schema for the next pg_engine setup to remove.
    async with pg_engine.begin() as conn:
        assert await conn.scalar(text("SELECT to_regnamespace('projection_audit')")) is None
        await conn.exec_driver_sql("CREATE SCHEMA projection_audit")
        await conn.exec_driver_sql("CREATE TABLE projection_audit.previous_test(id integer)")
        await conn.execute(
            text("INSERT INTO projection_audit.previous_test VALUES (:id)"), {"id": iteration}
        )


async def test_nullable_json_null_remains_distinct_from_sql_null(archive_db):
    from sqlalchemy import JSON, MetaData, Table, null

    from bfx_funding_bot.modules.execution.projection_cutover import codec

    factory, engine = archive_db
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO event_log(account_id,exchange_account_id,deployment_environment,event_type,payload,occurred_at_ms)
            SELECT :s,:a,'ci','CREDIT_CLOSED','{}'::jsonb,i FROM generate_series(1,3) i
        """),
            {"a": ACCOUNT, "s": str(ACCOUNT)},
        )
        conn.execute(
            text("""
            INSERT INTO execution_uncertainties(exchange_account_id,deployment_environment,symbol,kind,
              correlation_key,intended_amount,evidence,opened_event_seq)
            VALUES (:a,'ci','fUST','submit_outcome_unknown','sql-null',1,
              '{"nested": [null,"null",["json_null"],{"type": "json_null"}]}',1)
        """),
            {"a": ACCOUNT},
        )
        conn.execute(
            text("""
            INSERT INTO execution_uncertainties(exchange_account_id,deployment_environment,symbol,kind,
              correlation_key,intended_amount,evidence,opened_event_seq,state,reconcile_event_seq,
              resolved_event_seq,resolved_by_operator_id,resolution_reason,resolution_evidence,resolved_at)
            VALUES (:a,'ci','fUST','submit_outcome_unknown','json-null',1,'{}',1,'resolved',2,3,
              'synthetic','fixture','null'::jsonb,now())
        """),
            {"a": ACCOUNT},
        )
        for key, value in (
            ("json-string", '"null"'),
            ("json-list", '["json_null"]'),
            ("json-map", '{"tag":"json_null"}'),
        ):
            conn.execute(
                text("""
                INSERT INTO execution_uncertainties(exchange_account_id,deployment_environment,symbol,kind,
                  correlation_key,intended_amount,evidence,opened_event_seq,state,reconcile_event_seq,
                  resolved_event_seq,resolved_by_operator_id,resolution_reason,resolution_evidence,resolved_at)
                VALUES (:a,'ci','fUST','submit_outcome_unknown',:key,1,'{}',1,'resolved',2,3,
                  'synthetic','fixture',CAST(:value AS jsonb),now())
            """),
                {"a": ACCOUNT, "key": key, "value": value},
            )
        assert conn.execute(
            text(
                "SELECT correlation_key,resolution_evidence IS NULL,resolution_evidence::text FROM execution_uncertainties ORDER BY correlation_key"
            )
        ).all() == [
            ("json-list", False, '["json_null"]'),
            ("json-map", False, '{"tag": "json_null"}'),
            ("json-null", False, "null"),
            ("json-string", False, '"null"'),
            ("sql-null", True, None),
        ]
    expected = await capture(factory)
    with engine.connect() as conn:
        decoded = [
            decode_row(payload)
            for payload in conn.execute(
                text(
                    "SELECT encoded_payload FROM projection_audit.rows WHERE run_id=:id AND table_name='execution_uncertainties'"
                ),
                {"id": expected.run_id},
            ).scalars()
        ]
    evidence = {row["correlation_key"]: row["resolution_evidence"] for row in decoded}
    assert evidence["sql-null"] is None
    assert evidence["json-null"] is not None
    assert evidence["json-null"] is codec.JSON_NULL
    assert evidence["json-string"] == "null"
    assert evidence["json-list"] == ["json_null"]
    assert evidence["json-map"] == {"tag": "json_null"}
    assert next(row["evidence"] for row in decoded if row["correlation_key"] == "sql-null") == {
        "nested": [None, "null", ["json_null"], {"type": "json_null"}],
    }
    # Restore ALL fields into a real PG table with the original CHECK/NOT NULL
    # constraints. Only typed JSON columns need these SQL binding decisions.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TEMP TABLE restored_uncertainties (LIKE public.execution_uncertainties INCLUDING ALL)"
        )
        restored = Table("restored_uncertainties", MetaData(), autoload_with=conn)
        for row in decoded:
            values = dict(row)
            for column in restored.c:
                if isinstance(column.type, JSON):
                    if values[column.name] is codec.JSON_NULL:
                        values[column.name] = JSON.NULL
                    elif values[column.name] is None:
                        values[column.name] = null()
            conn.execute(restored.insert().values(values))
        assert conn.execute(
            text(
                "SELECT correlation_key,resolution_evidence IS NULL,resolution_evidence::text FROM restored_uncertainties ORDER BY correlation_key"
            )
        ).all() == [
            ("json-list", False, '["json_null"]'),
            ("json-map", False, '{"tag": "json_null"}'),
            ("json-null", False, "null"),
            ("json-string", False, '"null"'),
            ("sql-null", True, None),
        ]
        assert (
            conn.execute(
                text("SELECT to_jsonb(r) FROM restored_uncertainties r ORDER BY correlation_key")
            ).all()
            == conn.execute(
                text("SELECT to_jsonb(r) FROM execution_uncertainties r ORDER BY correlation_key")
            ).all()
        )


async def test_capture_source_spool_is_private_and_cleaned_on_success(
    archive_db, tmp_path, monkeypatch
):
    import stat
    from functools import partial
    from tempfile import TemporaryDirectory

    from sqlalchemy import event

    from bfx_funding_bot.modules.execution.projection_cutover import archive

    factory, _ = archive_db
    monkeypatch.setattr(archive, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path))
    checked = set()

    def inspect_source_files(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO projection_audit.rows"):
            for directory in tmp_path.iterdir():
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700
                database = directory / "source.sqlite"
                assert stat.S_IMODE(database.stat().st_mode) == 0o600
                checked.add(directory)

    event.listen(factory.kw["bind"].sync_engine, "before_cursor_execute", inspect_source_files)
    try:
        await capture(factory)
    finally:
        event.remove(factory.kw["bind"].sync_engine, "before_cursor_execute", inspect_source_files)
    assert checked
    assert list(tmp_path.iterdir()) == []


async def test_source_spool_is_removed_when_pg_insert_aborts(archive_db, tmp_path, monkeypatch):
    from functools import partial
    from tempfile import TemporaryDirectory

    from sqlalchemy.exc import DBAPIError

    from bfx_funding_bot.modules.execution.projection_cutover import archive

    factory, engine = archive_db
    monkeypatch.setattr(archive, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path))
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            CREATE FUNCTION public.reject_archive_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic insert failure'; END $$
        """)
        conn.exec_driver_sql(
            "CREATE TRIGGER zz_reject_archive_insert BEFORE INSERT ON projection_audit.rows FOR EACH ROW EXECUTE FUNCTION public.reject_archive_insert()"
        )
    with pytest.raises(DBAPIError, match="synthetic insert failure"):
        await capture(factory)
    assert list(tmp_path.iterdir()) == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM projection_audit.runs")).scalar_one() == 0
