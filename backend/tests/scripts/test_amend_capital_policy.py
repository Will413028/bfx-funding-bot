"""The owner's amendment script amends through the ledger's policy store, and only on a
database whose latest capital authority epoch is ``ledger``.

Mutations (one at a time; revert after each): the script skips the epoch read
(``test_a_legacy_epoch_refuses``), or the realm read (``test_an_unstamped_database_refuses``).
"""
from __future__ import annotations

import argparse
from uuid import UUID

import pytest
from sqlalchemy import text

# Every table the shared metadata may reach by foreign key, whatever was imported first.
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.uncertainty_tables  # noqa: F401
from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch, DatabaseRealmRow
from bfx_funding_bot.core.db import Base, make_async_engine_from_url, make_session_factory
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.policy_write import write_policy_revision
from bfx_funding_bot.modules.trading import CapitalPolicy
from scripts import amend_capital_policy as script

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")


def _args(**changes) -> argparse.Namespace:
    base = {
        "exchange_account_id": ACCOUNT, "environment": "ci", "symbol": "fUST", "enabled": False,
        "max_offer_amount": None, "min_period_days": None, "max_period_days": None,
        "max_open_offers": None, "rate_floor_ratio": None, "min_rate_apr": None,
        "apply_digest": None, "reason": None,
    }
    return argparse.Namespace(**{**base, **changes})


async def _epoch(session, seq: int, authority: str) -> None:
    await session.execute(text(
        "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
        "VALUES (:seq, :authority, :seq, 'test', 'test')"), {"seq": seq, "authority": authority})


@pytest.fixture
async def database(tmp_path, monkeypatch):
    """A database as it is at head: the seeded ``legacy`` epoch, then the ``ledger`` genesis."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'amend.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = make_async_engine_from_url(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory.begin() as session:
        session.add(DatabaseRealmRow(realm="ci", stamped_at_ms=1, actor="test"))
        await _epoch(session, 1, "legacy")
        await _epoch(session, 2, "ledger")
        await write_policy_revision(
            session, Scope(ACCOUNT, "ci"), symbol="fUST", policy=CapitalPolicy(enabled=True),
            expected_revision=0, source={"fixture": True})
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_script_amends_through_the_ledger_store(database) -> None:
    report = await script.run(_args())
    assert report["status"] == "dry_run" and report["new_policy"]["enabled"] is False
    applied = await script.run(_args(apply_digest=report["amendment_digest"],
                                     reason="maintenance window"))

    assert applied["status"] == "applied" and applied["new_revision"] == 2
    async with database() as session:
        from bfx_funding_bot.modules.ledger.tables import CapitalPolicyRevisionRow
        from bfx_funding_bot.modules.ledger.wiring import build_policy_store
        read = await build_policy_store(Scope(ACCOUNT, "ci")).read_applied(session, symbol="fUST")
        written = await session.get(CapitalPolicyRevisionRow, read.revision_id)
    assert (read.revision, read.policy.enabled) == (2, False)
    # No request stands behind an owner's revision: its source is where the reason lives.
    assert written.operator_request_id is None
    assert written.source["reason"] == "maintenance window"
    assert {"amendment_digest", "changes"} <= set(written.source)


def test_a_dry_run_needs_no_reason(monkeypatch, capsys) -> None:
    argv = ["amend_capital_policy", "--exchange-account-id", str(ACCOUNT), "--environment", "ci",
            "--symbol", "fUST", "--enabled", "false"]
    monkeypatch.setattr("sys.argv", argv)
    seen: list[argparse.Namespace] = []

    async def run(args: argparse.Namespace) -> dict[str, str]:
        seen.append(args)
        return {"status": "dry_run"}

    monkeypatch.setattr(script, "run", run)
    assert script.main() == 0
    assert seen and seen[0].reason is None


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_applying_without_a_reason_is_refused_before_anything_runs(monkeypatch, capsys, reason) -> None:
    argv = ["amend_capital_policy", "--exchange-account-id", str(ACCOUNT), "--environment", "ci",
            "--symbol", "fUST", "--enabled", "false", "--apply-digest", "d"]
    if reason is not None:
        argv += ["--reason", reason]
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(script, "run", lambda args: pytest.fail("ran without a reason"))
    with pytest.raises(SystemExit) as exit_:
        script.main()
    assert exit_.value.code == 2
    assert "--reason" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_legacy_epoch_refuses(database) -> None:
    """A database never switched (or restored from before the switch) is not the ledger's."""
    async with database.begin() as session:
        await _epoch(session, 3, "legacy")
    with pytest.raises(AuthorityMismatch, match="authority_unsupported value=legacy"):
        await script.run(_args())


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", ["prod", "shadow", "ci"])
async def test_every_known_realm_reads_the_epoch_against_the_ledger_alone(
    database, monkeypatch, realm: str,
) -> None:
    async with database.begin() as session:
        await session.execute(text("UPDATE database_realm SET realm = :realm"), {"realm": realm})
    seen: list[object] = []

    async def require(session: object) -> str:
        seen.append(session)
        raise AuthorityMismatch("stop after the read")

    monkeypatch.setattr(script, "require_ledger_authority", require)
    with pytest.raises(AuthorityMismatch):
        await script.run(_args())
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_unstamped_database_refuses(database) -> None:
    async with database.begin() as session:
        await session.execute(text("DELETE FROM database_realm"))
    with pytest.raises(DatabaseRealmMismatch, match="unstamped"):
        await script.run(_args())
