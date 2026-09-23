"""Run Alembic and exercise actual restricted roles on a disposable PostgreSQL."""
import asyncio
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration


def test_release_upgrade_acl_state_guards_and_auth_revocation(pg_container):
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        for role in ("bfx_bot", "bfx_webapi"):
            conn.exec_driver_sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role}; END IF; END $$")
            conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
            conn.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
    legacy = None
    for command in (["uv", "run", "alembic", "upgrade", "a9d3e5f7b102"],
                    ["uv", "run", "alembic", "upgrade", "head"],
                    ["uv", "run", "alembic", "upgrade", "head"],
                    ["uv", "run", "alembic", "check"]):
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[2],
            env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        if command[-1] == "a9d3e5f7b102":
            with engine.begin() as conn:
                conn.exec_driver_sql("INSERT INTO exchange_accounts(id,venue,label) VALUES ('00000000-0000-0000-0000-00000000ab11','bitfinex','historical-fixture')")
                halt_id = conn.scalar(text("""INSERT INTO trading_halt(account_id,exchange_account_id,deployment_environment,halted,reason,actor,created_at_ms)
                    VALUES ('00000000-0000-0000-0000-00000000ab11','00000000-0000-0000-0000-00000000ab11','ci',true,'historical','fixture',1000) RETURNING id"""))
                conn.execute(text("""INSERT INTO canary_command_permits(permit_id,halt_id,exchange_account_id,deployment_environment,symbol,cell,strategy,amount_usdt,operator_id,state,issued_at_ms,consumed_at_ms)
                    VALUES ('00000000-0000-0000-0000-00000000ab12',:halt_id,'00000000-0000-0000-0000-00000000ab11','ci','fUST','fUST_a30','mean_reversion',150,'historical-fixture','consumed',1000,1100)"""), {"halt_id": halt_id})
                legacy = conn.scalar(text("SELECT to_jsonb(p) FROM canary_command_permits p"))
    with engine.begin() as conn:
        assert "release_sessions" in inspect(conn).get_table_names()
        assert conn.scalar(text("SELECT to_jsonb(p) FROM canary_command_permits p")) == legacy
        for column in ("state", "binding", "permit_id", "consumed_at_ms", "promoted_halt_id"):
            assert not conn.scalar(text("SELECT has_column_privilege('bfx_webapi','release_sessions',:c,'UPDATE')"), {"c": column})
            assert not conn.scalar(text("SELECT has_column_privilege('bfx_webapi','release_sessions',:c,'INSERT')"), {"c": column})
        assert conn.scalar(text("SELECT has_column_privilege('bfx_webapi','release_sessions','requested_action','UPDATE')"))
        assert conn.scalar(text("SELECT has_column_privilege('bfx_bot','release_sessions','state','UPDATE')"))
        conn.exec_driver_sql("INSERT INTO exchange_accounts(id,venue,label) VALUES ('00000000-0000-0000-0000-00000000ab01','bitfinex','fixture')")
        conn.exec_driver_sql("INSERT INTO exchange_account_memberships(exchange_account_id,user_id,role) VALUES ('00000000-0000-0000-0000-00000000ab01','fixture','owner')")
        conn.exec_driver_sql('''INSERT INTO auth."user" (id,name,email,"emailVerified","createdAt","updatedAt",role,banned,"twoFactorEnabled") VALUES ('fixture','fixture','fixture@test.invalid',false,now(),now(),'admin',false,true)''')
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql("""INSERT INTO release_sessions(id,exchange_account_id,deployment_environment,symbol,cell,strategy,max_amount,expires_at_ms,created_at_ms,requested_by)
          VALUES ('00000000-0000-0000-0000-00000000ab02','00000000-0000-0000-0000-00000000ab01','ci','fUST','fUST_a30','mean_reversion',200,5000,1000,'fixture')""")
    for sql in ("UPDATE release_sessions SET state='promoted'", "UPDATE release_sessions SET max_amount=999"):
        with engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
            conn.exec_driver_sql(sql)
    with engine.begin() as conn, pytest.raises(Exception, match="release transition"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE release_sessions SET state='promoted'")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        assert conn.scalar(text("SELECT public.release_operator_authorized('00000000-0000-0000-0000-00000000ab01','fixture')"))
    async def restricted_handoff():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        # Register the referenced event/decision metadata used by ORM flush.
        import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
        from bfx_funding_bot.modules.execution.release_session import ReleaseSessions
        from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
        db = create_async_engine(url)
        factory = async_sessionmaker(db, expire_on_commit=False)
        account = UUID("00000000-0000-0000-0000-00000000ab01")
        repo = ReleaseSessions(account, "ci")
        binding = {"fixture": "measured", "config_digest": "test", "source_revision": "test"}
        halt = await HaltStateStore(factory, account_id=str(account), deployment_environment="ci").set_halted(True, reason="fixture", actor="fixture")
        try:
            async with factory.begin() as session:
                await session.execute(text("SET LOCAL ROLE bfx_webapi"))
                row = await repo.request(session, operator="fixture", symbol="fUST", cell="fUST_a30",
                    strategy="mean_reversion", max_amount=Decimal("200"), expires_at_ms=5000, now_ms=1000)
                sid = row.id
            async with factory.begin() as session:
                await session.execute(text("SET LOCAL ROLE bfx_bot"))
                row = await repo.prepare(session, sid, binding=binding, halt_id=halt.id,
                    minimum_amount=Decimal("153"), now_ms=1100)
                assert row.state == "prepared"
            async with factory.begin() as session:
                await session.execute(text("SET LOCAL ROLE bfx_webapi"))
                await repo.request_action(session, sid, action="authorize", operator="fixture", expected_revision=1, now_ms=1101)
            async with factory.begin() as session:
                await session.execute(text("SET LOCAL ROLE bfx_bot"))
                row = await repo.authorize(session, sid, binding=binding, now_ms=1102)
                assert row.state == "authorized"
            from tests.integration.test_capital_repository import intent
            event, decision = intent(account, "153")
            decision.strategy, decision.cell_id = "mean_reversion", "fUST_a30"
            async with factory.begin() as session:
                session.add(decision)
            async with factory.begin() as session:
                await session.execute(text("SET LOCAL ROLE bfx_bot"))
                row = await repo.consume(session, sid, binding=binding, decision=decision,
                    attempt_id=event.submission_attempt.attempt_id, now_ms=1103)
                assert row.state == "consumed"
            for sql, message in (
                ("UPDATE canary_command_permits SET amount_usdt=199", "immutable"),
                ("UPDATE release_sessions SET minimum_amount=199 WHERE state='consumed'", "immutable"),
                ("UPDATE release_sessions SET state='prepared' WHERE state='requested'", "ck_release_prepared"),
                ("UPDATE release_sessions SET state='prepared', binding='{}'::jsonb, halt_id=(SELECT max(id) FROM trading_halt), minimum_amount=NULL WHERE state='requested'", "ck_release_prepared"),
            ):
                with pytest.raises(Exception, match=message):
                    async with factory.begin() as session:
                        await session.execute(text("SET LOCAL ROLE bfx_bot"))
                        await session.execute(text(sql))
        finally:
            await db.dispose()
    asyncio.run(restricted_handoff())
    with engine.begin() as conn:
        conn.exec_driver_sql('UPDATE auth."user" SET banned=true WHERE id=\'fixture\'')
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        assert not conn.scalar(text("SELECT public.release_operator_authorized('00000000-0000-0000-0000-00000000ab01','fixture')"))
    result = subprocess.run(["uv", "run", "alembic", "downgrade", "a9d3e5f7b102"],
        cwd=Path(__file__).resolve().parents[2], env=dict(os.environ, DATABASE_URL=url),
        capture_output=True, text=True)
    assert result.returncode != 0
    assert "refuse downgrade of populated release evidence" in result.stderr
    engine.dispose()
