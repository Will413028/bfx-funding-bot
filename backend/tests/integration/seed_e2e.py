"""Helpers for the seed command's tests on a ``bot_e2e`` database.

The legacy bot that built a closure to seed is gone (S1-8); what is left drives the real seed
command (``apps.ledger_seed.run``) as the table owner against a migrated database, which
starts on the genesis ``ledger`` epoch unless a test puts it back before the switch.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from bfx_funding_bot.apps import ledger_seed
from bfx_funding_bot.modules.ledger.tables import CapitalAuthorityEpochRow

from .bot_e2e import SCOPE, BotEnv


def seed_command(env: BotEnv, tmp_path: Path, *, url: Any, run_id: str = "seed-e2e",
                 realm: str = "ci", authorize: bool = True,
                 scopes: tuple[str, ...] | None = None) -> list[str]:
    """argv + files for ``apps.ledger_seed``: a 0600 DSN file and a manifest naming it."""
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    parsed = make_url(dsn)
    dsn_file = tmp_path / f"{run_id}.dsn"
    dsn_file.write_text(dsn + "\n")
    os.chmod(dsn_file, 0o600)
    scope = f"{SCOPE.exchange_account_id}:{SCOPE.deployment_environment}"
    manifest = tmp_path / f"{run_id}.json"
    manifest.write_text(json.dumps({
        "mode": "seed", "run_id": run_id, "host": parsed.host, "port": parsed.port,
        "database": parsed.database, "user": parsed.username, "realm": realm,
        "scopes": [scope],
    }))
    argv = ["--dsn-file", str(dsn_file), "--manifest", str(manifest), "--run-id", run_id]
    for item in scopes if scopes is not None else (scope,):
        argv += ["--scope", item]
    if authorize:
        argv.append("--authorize-seed")
    return argv


async def run_seed(argv: list[str], *, now_ms: int, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
    output = io.StringIO()
    code = await ledger_seed.run(argv, output=output, clock=lambda: now_ms, **kwargs)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


async def pre_switch(env: BotEnv) -> None:
    """The owner appends a ``legacy`` epoch: the database reads as one before the switch."""
    async with env.factory.begin() as session:
        latest = await session.scalar(
            select(CapitalAuthorityEpochRow.epoch_seq).order_by(
                CapitalAuthorityEpochRow.epoch_seq.desc()).limit(1))
        session.add(CapitalAuthorityEpochRow(
            epoch_seq=int(latest or 0) + 1, authority="legacy", set_at_ms=1,
            actor="seed-test", reason="a database before the switch", evidence=None))


async def table_count(env: BotEnv, name: str) -> int:
    async with env.factory() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {name}")) or 0)


__all__ = ["pre_switch", "run_seed", "seed_command", "table_count"]
