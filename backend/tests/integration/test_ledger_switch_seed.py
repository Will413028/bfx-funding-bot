"""``apps.ledger_seed --switch`` and ``--check`` against the real schema (PR-3, switch pre-flight §F).

``--switch``: the seed rows and the ``ledger`` epoch row commit together or not at all. The
capture-point closure verifier runs on the seed's own session before the epoch append, and the
READ ONLY digest verification covers the epoch row. ``--check``: one READ ONLY transaction
without locks; it reports refusals and the F7 exposure and writes nothing.

Mutations (apply one at a time, run this file, revert; the test that fails is named):

* the epoch appended in its own transaction after the seed commits:
  ``test_an_epoch_append_failure_rolls_the_seed_back`` and
  ``test_a_tampered_epoch_row_is_a_digest_mismatch``;
* ``seed`` skips ``verify_capture_point``: ``test_a_closure_violation_rolls_everything_back``;
* ``with_epoch`` not applied (epoch left out of the expected digests): the success test (exit 2);
* ``check`` takes the writer locks (``quiesce``): ``test_check_writes_nothing_and_takes_no_lock``.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.apps import ledger_seed as seed_app
from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
from bfx_funding_bot.modules.ledger.tables import (
    LEDGER_TABLES,
    CapitalAuthorityEpochRow,
    LedgerObservationRow,
)

from .bot_e2e import (
    T0,
    BotEnv,
    bot_env,  # noqa: F401 - fixture
    ledger_db,  # noqa: F401 - fixture dependency
)
from .seed_e2e import credit_row, flip_epoch, run_seed, seed_command, table_count
from .test_ledger_seed_e2e import LOAN, RECENT_CREDIT, SEED_AT, run_legacy
from .test_ledger_seed_refusals import legacy_with_live_offer, stop_process

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def authority() -> str:
    return "legacy"


def switch_command(env: BotEnv, tmp_path: Any, url: Any, **kwargs: Any) -> list[str]:
    return [*seed_command(env, tmp_path, url=url, **kwargs), "--switch", "--cells",
            str(env.cells_path)]


def check_command(env: BotEnv, tmp_path: Any, url: Any, **kwargs: Any) -> list[str]:
    argv = seed_command(env, tmp_path, url=url, authorize=False, **kwargs)
    return [*argv, "--check", "--cells", str(env.cells_path)]


async def epochs(env: BotEnv) -> list[CapitalAuthorityEpochRow]:
    async with env.factory() as session:
        return list(await session.scalars(
            select(CapitalAuthorityEpochRow).order_by(CapitalAuthorityEpochRow.epoch_seq)))


async def assert_nothing_committed(env: BotEnv) -> None:
    for table in LEDGER_TABLES:
        assert await table_count(env, table.name) == 0, table.name
    assert [row.authority for row in await epochs(env)] == ["legacy"]


async def test_switch_commits_the_seed_and_the_ledger_epoch_together(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """Every closure case (recent_fill groups, claim-only, tail, open UNKNOWN, requests): the
    closure verifier is clean at the capture point, the epoch row carries the run id, the
    seed's ids and digests, the closure summary and F7; the verification covers the epoch."""
    env = bot_env
    await run_legacy(env)
    async with env.factory() as session:
        final = await session.scalar(select(CapitalSnapshotRow).order_by(
            CapitalSnapshotRow.event_seq.desc()).limit(1))
    assert final is not None
    values = final.classification["symbols"]["fUST"]
    total = sum((Decimal(values[k]) for k in ("available", "offered", "credits")), Decimal(0))

    code, lines = await run_seed(switch_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT)
    assert code == seed_app.EXIT_OK, lines
    (seeded,) = [line for line in lines if line["kind"] == "seed"]
    closure = [line for line in lines if line["kind"] == "closure"]
    assert closure and all(line["status"] == "ok" for line in closure), [
        line for line in closure if line["violations"]]
    (closure_summary,) = [line for line in lines if line["kind"] == "closure_summary"]
    assert closure_summary["passed"] is True and closure_summary["coverage_complete"] is True
    (verification,) = [line for line in lines if line["kind"] == "verification"]
    assert verification["mismatches"] == []
    assert lines[-1] == {"kind": "summary", "mode": "switch", "exit_code": 0,
                         "run_id": "seed-e2e", "committed": True}

    recent = LOAN + RECENT_CREDIT
    share = format((recent / total).quantize(Decimal("0.000001")), "f")
    assert seeded["recent_fill"] == {
        "symbols": {"fUST": {"recent_fill": format(recent, "f"),
                             "total_capital": format(total, "f"), "share": share}},
        "max_share": share,
    }

    legacy, ledger = await epochs(env)
    assert (legacy.authority, ledger.authority) == ("legacy", "ledger")
    assert ledger.epoch_seq == legacy.epoch_seq + 1
    assert (ledger.actor, ledger.set_at_ms) == ("ledger_seed:seed-e2e", SEED_AT)
    evidence = ledger.evidence
    assert evidence is not None and evidence["run_id"] == "seed-e2e"
    assert evidence["closure"] == {"checks": closure_summary["checks"], "violations": 0,
                                   "passed": True}
    (seed_evidence,) = evidence["seeds"]
    assert (seed_evidence["observation_id"], seed_evidence["basis_id"]) == (
        seeded["observation_id"], seeded["basis_id"])
    assert seed_evidence["recent_fill"] == seeded["recent_fill"]
    assert seed_evidence["digests"] == {
        name: digest["sha256"] for name, digest in seeded["expected"].items()
        if name != "capital_authority_epoch"}
    (epoch_line,) = [line for line in lines if line["kind"] == "epoch"]
    assert epoch_line["epoch_seq"] == ledger.epoch_seq and epoch_line["evidence"] == evidence
    async with env.factory() as session:
        origins = list(await session.scalars(select(LedgerObservationRow.origin)))
    assert origins == ["legacy_seed"]


async def test_a_closure_violation_rolls_everything_back(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """The verifier sees a seed that is not the legacy closure (here: a trading_state
    watermark that differs): exit 4, no ledger rows, no epoch row."""
    env = bot_env
    await legacy_with_live_offer(env)
    honest = seed_app.seed_evidence

    def wrong_watermark(item: Any) -> Any:
        evidence = honest(item)
        return replace(evidence, trading_state_max_id=(evidence.trading_state_max_id or 0) + 1)

    monkeypatch.setattr(seed_app, "seed_evidence", wrong_watermark)
    code, lines = await run_seed(switch_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT)
    assert code == seed_app.EXIT_CLOSURE, lines
    violations = {v["reason"] for line in lines if line["kind"] == "closure"
                  for v in line["violations"]}
    assert violations, lines
    assert not [line for line in lines if line["kind"] in {"epoch", "verification"}]
    assert lines[-1]["committed"] is False
    await assert_nothing_committed(env)


async def test_a_seed_digest_mismatch_leaves_no_epoch_row(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)
    honest = seed_app.write_seed

    async def misstated(session: Any, closure: Any) -> Any:
        """Writes honestly, then states a wrong expected digest for the clock table."""
        result = await honest(session, closure)
        expected = dict(result.expected)
        clock = expected["capital_command_clock"]
        expected["capital_command_clock"] = replace(clock, sha256="0" * 64)
        return replace(result, expected=expected)

    monkeypatch.setattr(seed_app, "write_seed", misstated)
    code, lines = await run_seed(switch_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT)
    assert code == seed_app.EXIT_DIGEST, lines
    (verification,) = [line for line in lines if line["kind"] == "verification"]
    assert {m["table"] for m in verification["mismatches"]} == {"capital_command_clock"}
    assert [line["kind"] for line in lines].count("epoch") == 1  # appended, then rolled back
    await assert_nothing_committed(env)


async def test_a_tampered_epoch_row_is_a_digest_mismatch(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """The row written differs from the row planned: the verification names the epoch table,
    everything rolls back (the epoch row is inside the verified transaction)."""
    env = bot_env
    await legacy_with_live_offer(env)
    honest = seed_app.append_switch_epoch

    async def tampered(session: Any, row: dict[str, object]) -> None:
        await honest(session, {**row, "reason": "something else"})

    monkeypatch.setattr(seed_app, "append_switch_epoch", tampered)
    code, lines = await run_seed(switch_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT)
    assert code == seed_app.EXIT_DIGEST, lines
    (verification,) = [line for line in lines if line["kind"] == "verification"]
    assert {m["table"] for m in verification["mismatches"]} == {"capital_authority_epoch"}
    await assert_nothing_committed(env)


async def test_an_epoch_append_failure_rolls_the_seed_back(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)
    honest = seed_app.append_switch_epoch

    async def failing(session: Any, row: dict[str, object]) -> None:
        await honest(session, row)
        raise RuntimeError("epoch append failed")

    monkeypatch.setattr(seed_app, "append_switch_epoch", failing)
    code, lines = await run_seed(switch_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "operational_failure"), lines
    await assert_nothing_committed(env)


async def test_a_refusal_writes_no_row_and_no_epoch(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    env.venue.wallets = [["funding", "UST", "5000", 0, "5000"]]
    row = credit_row(8201, Decimal("100"), T0 - 5_000)
    row[13] = None  # no MTS_OPENING: the ledger cannot key it
    env.venue.credits = [row]
    daemon = await env.build()
    await env.boot(daemon, T0)
    await stop_process(daemon)
    code, lines = await run_seed(switch_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "credit_opening_unkeyable")
    await assert_nothing_committed(env)


async def test_switch_requires_the_cells_file(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)
    argv = [*seed_command(env, tmp_path, url=ledger_db.url), "--switch"]
    code, lines = await run_seed(argv, now_ms=SEED_AT)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "cells_required")
    missing = [*seed_command(env, tmp_path, url=ledger_db.url), "--switch", "--cells",
               str(tmp_path / "absent.yaml")]
    code, lines = await run_seed(missing, now_ms=SEED_AT)
    assert (code, lines[-1]["reason"]) == (seed_app.EXIT_REFUSED, "cells_unreadable")
    await assert_nothing_committed(env)


class Statements:
    """Every SQL statement a ``--check`` run sends, captured on its engine."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def connector(self, plan: Any) -> AsyncEngine:
        engine = seed_app.connect(plan)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            self.sql.append(" ".join(statement.split()))

        return engine


READ_PREFIXES = ("SELECT", "WITH", "SET TRANSACTION READ ONLY", "SET LOCAL search_path",
                 "BEGIN", "COMMIT", "ROLLBACK", "SHOW")


async def test_check_writes_nothing_and_takes_no_lock(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """Legacy still runs (its writer lock is held, its session connected): the check passes
    without a lock or a write, and reports the F7 exposure."""
    env = bot_env
    daemon = await legacy_with_live_offer(env, stop=False)
    statements = Statements()
    code, lines = await run_seed(check_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT,
                                 connector=statements.connector)
    assert code == seed_app.EXIT_OK, lines
    (scope_line,) = [line for line in lines if line["kind"] == "check"]
    assert scope_line["refusal"] is None
    assert scope_line["recent_fill"]["symbols"]["fUST"]["recent_fill"] == "0"
    assert lines[-1] == {"kind": "summary", "mode": "check", "exit_code": 0,
                         "run_id": "seed-e2e", "committed": False, "refusals": []}
    assert statements.sql
    offending = [sql for sql in statements.sql if not sql.upper().startswith(
        tuple(p.upper() for p in READ_PREFIXES))]
    assert offending == []
    assert not [sql for sql in statements.sql if "advisory" in sql or "FOR UPDATE" in sql.upper()
                or "FOR SHARE" in sql.upper()]
    assert any(sql == "SET TRANSACTION READ ONLY" for sql in statements.sql)
    await assert_nothing_committed(env)
    await stop_process(daemon)


async def test_check_reports_every_refusal(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    """A credit the ledger cannot key, then (after a seed and a flip) the snapshot guards:
    each is reported, nothing is written."""
    env = bot_env
    env.venue.wallets = [["funding", "UST", "5000", 0, "5000"]]
    row = credit_row(8201, Decimal("100"), T0 - 5_000)
    row[13] = None
    env.venue.credits = [row]
    daemon = await env.build()
    await env.boot(daemon, T0)
    code, lines = await run_seed(check_command(env, tmp_path, ledger_db.url), now_ms=SEED_AT)
    assert code == seed_app.EXIT_REFUSED, lines
    assert [(r["reason"], r["scope"] is None) for r in lines[-1]["refusals"]] == [
        ("credit_opening_unkeyable", False)]
    await assert_nothing_committed(env)
    await stop_process(daemon)


async def test_check_reports_the_snapshot_guards_together(
    bot_env: BotEnv, ledger_db: Any, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    await legacy_with_live_offer(env)
    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0, lines
    await flip_epoch(env, at=SEED_AT + 1)
    async with env.factory() as session:
        before = await session.scalar(text("SELECT count(*) FROM ledger_observation"))
    code, lines = await run_seed(check_command(env, tmp_path, ledger_db.url, run_id="check-2"),
                                 now_ms=SEED_AT + 2)
    assert code == seed_app.EXIT_REFUSED, lines
    assert [r["reason"] for r in lines[-1]["refusals"] if r["scope"] is None] == [
        "epoch_not_legacy", "ledger_not_empty"]
    async with env.factory() as session:
        after = await session.scalar(text("SELECT count(*) FROM ledger_observation"))
    assert after == before
