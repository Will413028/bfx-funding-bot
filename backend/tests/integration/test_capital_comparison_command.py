"""End-to-end command against isolated PostgreSQL with a SELECT-only login."""

import io
import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from bfx_funding_bot.apps import capital_comparison as command
from bfx_funding_bot.apps.capital_comparison_guard import AUTHORITY_TABLES
from tests.integration.test_trading_shadow_candidate import candidate_db as seeded_candidate_db

candidate_db = seeded_candidate_db

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def reader_url(pg_engine):
    role = "comparison_" + uuid4().hex
    # Same isolated-role lifecycle as test_trading_shadow_candidate, with LOGIN
    # so the full command (including current_user verification) opens its own connection.
    async with pg_engine.begin() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'test-only\' '
                                'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT SELECT ON {", ".join(AUTHORITY_TABLES)} TO "{role}"'))
    try:
        yield pg_engine.url.set(username=role, password="test-only")
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))


def args(tmp_path, url, account):
    manifest = tmp_path / "cutover.json"
    manifest.write_text(json.dumps({
        "mode": "cutover", "host": url.host, "port": url.port, "database": url.database,
        "user": url.username, "run_id": "integration",
    }))
    dsn_file = tmp_path / "dsn"
    dsn_file.write_text(url.render_as_string(hide_password=False))
    dsn_file.chmod(0o600)
    return ["--mode", "cutover", "--authorize-cutover-read", "--cutover-manifest", str(manifest),
            "--dsn-file", str(dsn_file), "--run-id", "integration",
            "--code-revision", "integration", "--scope", f"{account}:ci", "--cells",
            str(Path(__file__).parents[2] / "configs/cells.live.yaml")]


async def test_select_only_matching_scope_equal(candidate_db, reader_url, tmp_path, monkeypatch):
    _, repo, _, _ = candidate_db
    monkeypatch.setattr(command.time, "time_ns", lambda: 2_000_000_000)
    output = io.StringIO()
    assert await command.run(args(tmp_path, reader_url, repo.account_id), output=output) == 0
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [row["status"] for row in rows[:-1]] == ["equal", "equal"]
    assert all(row["candidate"]["view"] for row in rows[:-1])
    assert rows[-1]["coverage_complete"] is True
    assert rows[-1]["exit_code"] == 0


async def test_select_only_missing_policy_is_reported(candidate_db, reader_url, tmp_path, monkeypatch):
    _, repo, _, _ = candidate_db
    monkeypatch.setattr(command.time, "time_ns", lambda: 2_000_000_000)
    argv = args(tmp_path, reader_url, repo.account_id)
    absent = uuid4()
    argv += ["--scope", f"{absent}:ci"]
    output = io.StringIO()
    assert await command.run(argv, output=output) == 1
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    missing = [row for row in rows[:-1] if row["scope"]["account_id"] == str(absent)]
    assert len(missing) == 2
    assert all(row["status"] == "not_comparable" and row["reason"] == "policy_missing"
               and row["evidence"] for row in missing)
    assert rows[-1]["expected_scopes"] == rows[-1]["emitted_scopes"] == 4
    assert rows[-1]["exit_code"] == 1


async def test_owner_rejected_by_actual_privilege_check(candidate_db, pg_engine, tmp_path, monkeypatch):
    _, repo, _, _ = candidate_db
    verified = []
    original = command.verify_connection

    async def verify(session, plan):
        verified.append((await session.execute(text("SELECT current_user"))).scalar_one())
        await original(session, plan)

    monkeypatch.setattr(command, "verify_connection", verify)
    output = io.StringIO()
    assert await command.run(args(tmp_path, pg_engine.url, repo.account_id), output=output) == 3
    assert verified == [pg_engine.url.username]
    assert json.loads(output.getvalue())["exit_code"] == 3
