import asyncio
import base64
import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/bootstrap_account_credential.py"


@pytest.mark.parametrize(
    "args",
    [
        ["--api-secret", "SENSITIVE-SENTINEL"],
        ["--exchange-account-id", "SENSITIVE-SENTINEL", "--owner-user-id", "owner"],
        [
            "--exchange-account-id",
            "550e8400-e29b-41d4-a716-446655440000",
            "--owner-user-id",
            "owner",
        ],
    ],
)
def test_cli_failures_never_echo_secret_or_traceback(args):
    env = {
        **os.environ,
        "BFX_BOOTSTRAP_API_KEY": "",
        "BFX_BOOTSTRAP_API_SECRET": "SENSITIVE-SENTINEL",
        "BFX_VAULT_KEK": "SENSITIVE-SENTINEL",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 2
    assert "SENSITIVE-SENTINEL" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert json.loads(result.stderr)["status"] == "bootstrap_failed"


def test_help_explains_opt_in_apply_without_secret_arguments():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0
    assert "--apply" in result.stdout
    assert "--api-secret" not in result.stdout


def test_ambiguous_apply_failure_does_not_claim_no_commit(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("bootstrap_cli", SCRIPT)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    async def uncertain_commit(args):
        raise RuntimeError("SENSITIVE-SENTINEL")

    monkeypatch.setattr(cli, "_run", uncertain_commit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap",
            "--exchange-account-id",
            "550e8400-e29b-41d4-a716-446655440000",
            "--owner-user-id",
            "owner",
            "--apply",
        ],
    )
    previous_disable = logging.root.manager.disable
    try:
        assert cli.main() == 2
    finally:
        logging.disable(previous_disable)
    output = capsys.readouterr()
    report = json.loads(output.err)
    assert report.get("applied") is not False
    assert report["outcome"] == "unconfirmed"
    assert "SENSITIVE-SENTINEL" not in output.err + output.out


@pytest.mark.parametrize(
    "fault,apply,expected_count",
    [
        ("none", False, 0),
        ("none", True, 1),
        ("flush", True, 0),
        ("commit", True, 0),
        ("http", True, 0),
        ("cleanup", True, 1),
    ],
)
def test_real_cli_transaction_and_dependency_errors_are_secret_safe(
    tmp_path,
    monkeypatch,
    capsys,
    caplog,
    fault,
    apply,
    expected_count,
):
    from uuid import UUID

    import httpx
    from sqlalchemy import event, func, select
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

    from bfx_funding_bot.core import db
    from bfx_funding_bot.core.db import Base
    from bfx_funding_bot.modules.accounts.exchange_accounts import grant_membership
    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount, ExchangeAccountCredential

    spec = importlib.util.spec_from_file_location("bootstrap_cli_fault", SCRIPT)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    account = UUID("550e8400-e29b-41d4-a716-446655440000")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/vault.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory.begin() as session:
            session.add(
                ExchangeAccount(
                    id=account, venue="bitfinex", label="fixture", lifecycle_status="active"
                )
            )
            await session.flush()
            await grant_membership(
                session, exchange_account_id=account, user_id="owner", role="owner"
            )

    asyncio.run(seed())
    values = {
        "BFX_BOOTSTRAP_API_KEY": "KEY-SENTINEL",
        "BFX_BOOTSTRAP_API_SECRET": "SECRET-SENTINEL",
        "BFX_VAULT_KEK": base64.b64encode(bytes(range(32))).decode(),
        "DATABASE_URL": "postgresql://USER-SENTINEL:PASSWORD-SENTINEL@invalid/fixture",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    leak = " ".join(values.values())
    monkeypatch.setattr(db, "make_async_engine_from_url", lambda url: engine)

    def transport(request):
        assert request.url.path == "/v2/auth/r/permissions"
        assert request.headers["bfx-apikey"] == "KEY-SENTINEL"
        logging.getLogger("httpx").critical(leak)
        if fault == "http":
            return httpx.Response(500, text=leak)
        return httpx.Response(200, json=[["funding", 1, 1]])

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs),
    )

    def fail_flush(conn, cursor, statement, parameters, context, many):
        if "INSERT INTO exchange_account_credentials" in statement:
            logging.getLogger("sqlalchemy.engine").critical(leak)
            raise RuntimeError(leak)

    def fail_commit(conn):
        raise RuntimeError(leak)

    if fault == "flush":
        event.listen(engine.sync_engine, "before_cursor_execute", fail_flush)
    if fault == "commit":
        event.listen(engine.sync_engine, "commit", fail_commit)
    original_dispose = AsyncEngine.dispose

    async def fail_dispose(self, close=True):
        await original_dispose(self, close=close)
        raise RuntimeError(leak)

    if fault == "cleanup":
        monkeypatch.setattr(AsyncEngine, "dispose", fail_dispose)
    monkeypatch.setattr(
        sys,
        "argv",
        ["bootstrap", "--exchange-account-id", str(account), "--owner-user-id", "owner"]
        + (["--apply"] if apply else []),
    )
    previous_disable = logging.root.manager.disable
    try:
        result = cli.main()
    finally:
        logging.disable(previous_disable)
        if fault == "commit":
            event.remove(engine.sync_engine, "commit", fail_commit)
    output = capsys.readouterr()
    assert result == (0 if fault == "none" else 2)
    for value in values.values():
        assert value not in output.out + output.err + caplog.text
    assert "Traceback" not in output.out + output.err
    if fault != "none":
        assert json.loads(output.err)["outcome"] == "unconfirmed"

    async def count_rows():
        async with factory() as session:
            return await session.scalar(select(func.count()).select_from(ExchangeAccountCredential))

    try:
        assert asyncio.run(count_rows()) == expected_count
    finally:
        asyncio.run(original_dispose(engine))
