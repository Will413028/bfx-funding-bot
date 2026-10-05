"""Cutover comparison end to end (S1-4e): the ``ledger_reader`` and ``closure`` arms.

See ``cutover_e2e`` for the composition. Every run is the real command under the runbook reader
shape (a NOINHERIT LOGIN, ``SET LOCAL ROLE bfx_cutover_reader``), so a missing column grant
surfaces as an ``error`` arm or a failed run.

Mutations (apply one at a time, run this file, revert; the test that fails is named):

* ``_both`` returns only the left-only side (anti-join one direction):
  ``test_each_discrepancy_fails_the_run[legacy_group_removed]``;
* ``finish_cutover`` ignores the ``ledger_reader`` arm in the exit code:
  ``...[ledger_cell_changed]`` and ``...[legacy_value_perturbed]``;
* the command's transaction without ``READ ONLY``: ``test_seeded_state_compares_equal_under_the_reader``
  (the guard's attestation refuses it, exit 3);
* ``declare_divergence`` returns its evidence without re-checking the adjusted legacy answer:
  ``test_f7_divergence_with_any_other_difference_stays_different``.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bfx_funding_bot.apps.capital_comparison_closure import parse_seed_evidence, verify_closure
from bfx_funding_bot.apps.capital_comparison_guard import READER_ROLE
from bfx_funding_bot.modules.ledger.wiring import (
    build_ledger_managed_offers,
    build_ledger_uncertainties,
)
from bfx_funding_bot.modules.live_validation.tables import FundingTradeRow
from bfx_funding_bot.modules.trading import CapitalScope

from .bot_e2e import (
    CELL,
    SCOPE,
    BotEnv,
    bot_env,  # noqa: F401 - fixture
    ledger_db,  # noqa: F401 - fixture dependency
)
from .cutover_e2e import (
    PASSWORD,
    Cutover,
    build_cutover,
    by_kind,
    owner,
    reader_login,
    run_comparison,
)
from .seed_e2e import CELL_B
from .test_ledger_seed_e2e import RECENT_OPENING

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCOPES = [CapitalScope(SCOPE.exchange_account_id, "ci", "fUST", cell) for cell in (CELL, CELL_B)]


@pytest.fixture
def authority() -> str:
    return "legacy"


def arms(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["scope"]["cell_id"]: row for row in by_kind(rows, "arm")}


def closure_reasons(rows: list[dict[str, Any]]) -> set[str]:
    return {v["reason"] for row in by_kind(rows, "closure") for v in row["violations"]}


async def capture_point_closure(db: Any, seed_lines: list[dict[str, Any]]) -> list[Any]:
    """The verifier at the capture point (seeded, no runner yet), as the attested reader."""
    login = reader_login(db)
    url = db.url.set(drivername="postgresql+asyncpg", username=login, password=PASSWORD)
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await session.execute(text(f"SET LOCAL ROLE {READER_ROLE}"))
            return list(await verify_closure(
                session, scopes=SCOPES,
                seeds=parse_seed_evidence(json.dumps(line) for line in seed_lines),
                managed_offers=build_ledger_managed_offers(),
                uncertainties=build_ledger_uncertainties(),
            ))
    finally:
        await engine.dispose()


async def test_seeded_state_compares_equal_under_the_reader(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    """G5: equal on the seeded e2e state (live, claim-only, filled, recent_fill, loan->split,
    fill during the halt, legacy tail, requests, auto HALTED); closure clean at the capture point
    and after the runner; values compared (no open UNKNOWN blocks fUST)."""
    captured: list[Any] = []
    state = await build_cutover(bot_env, ledger_db, monkeypatch, tmp_path, unknown=False,
                                at_capture=lambda lines: _capture(ledger_db, lines, captured))
    assert captured and all(check.violations == () for check in captured), [
        (c.check, c.violations) for c in captured if c.violations]
    assert {c.check: c.evidence.get("mirror_check") for c in captured}["live_offers"] == "capture_point"

    code, rows = await run_comparison(state)
    summary = rows[-1]
    assert code == 0, summary
    by_cell = arms(rows)
    assert {cell: row["status"] for cell, row in by_cell.items()} == {CELL: "equal", CELL_B: "equal"}
    for row in by_cell.values():
        assert row["legacy"]["kind"] == row["ledger"]["kind"] == "available"
        assert row["legacy"] == row["ledger"]
    # The fill during the halt and the loan split are attributed to the same cells by both arms.
    assert by_cell[CELL_B]["ledger"]["snapshot.cell_exposure"] != by_cell[CELL]["ledger"]["snapshot.cell_exposure"]
    closure = by_kind(rows, "closure")
    assert all(row["status"] == "ok" for row in closure), [r for r in closure if r["violations"]]
    assert {row["check"]: row["evidence"].get("mirror_check") for row in closure}["live_offers"] == (
        "after_capture_point")
    assert summary["arms"]["ledger_reader"]["passed"] and summary["arms"]["closure"]["passed"]
    assert summary["window"]["ok"] and summary["inventory_status"] == "ok"
    assert summary["provenance"]["now_ms"] == state.as_of


async def _capture(db: Any, lines: list[dict[str, Any]], into: list[Any]) -> None:
    into.extend(await capture_point_closure(db, lines))


async def test_open_unknown_blocks_both_arms_alike_and_the_closure_holds(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    """The full S1-4d closure (open UNKNOWN, failed uncertainty request): both arms refuse fUST
    with ``execution_unknown`` (a conclusive block), the closure is clean."""
    state = await build_cutover(bot_env, ledger_db, monkeypatch, tmp_path, unknown=True)
    code, rows = await run_comparison(state)
    assert code == 0, rows[-1]
    for row in arms(rows).values():
        assert row["status"] == "equal"
        assert row["legacy"] == row["ledger"] == {"kind": "blocked", "reason": "execution_unknown"}
    assert closure_reasons(rows) == set()
    requests = next(r for r in by_kind(rows, "closure") if r["check"] == "requests")
    assert requests["evidence"]["failed"] == 1


def _perturb_wallet(observation: dict[str, Any]) -> dict[str, Any]:
    changed = copy.deepcopy(observation)
    for name in ("event", "confirmation"):
        wallets = changed["observations"][0][name]["wallet_available"]
        wallets["fUST"] = str(Decimal(wallets["fUST"]) + 1)
    return changed


def _without_triggers(table: str, *statements: str) -> tuple[str, ...]:
    return (f"ALTER TABLE {table} DISABLE TRIGGER USER", *statements,
            f"ALTER TABLE {table} ENABLE TRIGGER USER")


def _inject(state: Cutover, name: str) -> dict[str, Any]:
    """Apply one discrepancy; returns command overrides (an altered input file)."""
    seed = next(line for line in state.seed_lines if line["kind"] == "seed")
    basis, account = seed["basis_id"], SCOPE.exchange_account_id
    db = state.ledger_db
    if name == "seed_credit_removed":  # a seeded row removed: legacy group without seed row
        owner(db, *_without_triggers("accepted_capital_basis_credit_cell",
              f"DELETE FROM accepted_capital_basis_credit_cell WHERE basis_id = '{basis}' "
              "AND venue_credit_id = '8003'"),
              *_without_triggers("accepted_capital_basis_credit",
              f"DELETE FROM accepted_capital_basis_credit WHERE basis_id = '{basis}' "
              "AND venue_credit_id = '8003'"))
    elif name == "legacy_group_removed":  # the reverse direction: seed group without legacy
        owner(db, *_without_triggers("capital_snapshots",
              "UPDATE capital_snapshots SET classification = classification #- '{credit_cells,8003}' "
              "WHERE event_seq = (SELECT max(event_seq) FROM capital_snapshots)"))
    elif name == "seed_cell_changed":  # a seeded cell set changed
        owner(db, *_without_triggers("accepted_capital_basis_credit_cell",
              f"UPDATE accepted_capital_basis_credit_cell SET cell_id = '{CELL_B}' "
              f"WHERE basis_id = '{basis}' AND venue_credit_id = '8003'"))
    elif name == "ledger_cell_changed":  # the runner basis's cell exposure changed
        owner(db, *_without_triggers("accepted_capital_basis_cell",
              f"UPDATE accepted_capital_basis_cell SET amount = amount + 1 WHERE cell_id = '{CELL}' "
              f"AND basis_id <> '{basis}'"))
    elif name == "fingerprint_extra":  # a legacy pending claim no ledger row holds
        owner(db, "INSERT INTO offer_claims (cid, account_id, exchange_account_id, "
              "deployment_environment, state, venue_offer_id, symbol, size_usdt, "
              "signal_correlation_id, execution_decision_id, occurred_at_ms, last_updated_ms, "
              f"last_event_seq) VALUES (990001, '{account}', '{account}', 'ci', 'pending', NULL, "
              f"'fUST', 77.00000999, '{uuid4()}', NULL, 1, 1, 1)")
    elif name == "unlisted_scope":  # authority state of a scope the run was not given
        other = uuid4()
        owner(db, f"INSERT INTO exchange_accounts (id, venue, label) VALUES ('{other}', 'bitfinex', 'x')",
              *_without_triggers("trading_state",
              "INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause, "
              f"actor, reason, created_at_ms) VALUES ('{other}', 'ci', 'HALTED', 'auto', 'x', 'x', 1)"))
    elif name == "trading_state_moved":
        owner(db, *_without_triggers("trading_state",
              "INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause, "
              f"actor, reason, created_at_ms) VALUES ('{account}', 'ci', 'HALTED', 'auto', 'x', 'x', 1)"))
    elif name == "failed_request_not_terminal":
        request = seed["failed_uncertainty_requests"][0]
        owner(db, *_without_triggers("uncertainty_resolution_requests",
              "UPDATE uncertainty_resolution_requests SET outcome_reason = 'expired' "
              f"WHERE request_id = '{request}'"))
    elif name == "cell_without_policy":  # a listed cell whose symbol has no policy head
        return {"extra_cells": ("fBTC_a30",)}
    elif name == "legacy_value_perturbed":
        return {"observation": _perturb_wallet(state.observation)}
    else:
        raise AssertionError(name)
    return {}


# injection -> (needs the open UNKNOWN closure, expected closure reasons, expected arm statuses,
# inventory status)
DISCREPANCIES: dict[str, tuple[bool, set[str], dict[str, str] | None, str]] = {
    "seed_credit_removed": (False, {"legacy_group_not_seeded"}, None, "ok"),
    "legacy_group_removed": (False, {"seeded_group_not_legacy"}, None, "ok"),
    "seed_cell_changed": (False, {"credit_group_differs"}, None, "ok"),
    "ledger_cell_changed": (False, set(), {CELL: "different", CELL_B: "equal"}, "ok"),
    "fingerprint_extra": (False, {"fingerprint_legacy_only"}, None, "ok"),
    "unlisted_scope": (False, set(), None, "not_comparable"),
    "trading_state_moved": (False, {"trading_state_moved"}, None, "ok"),
    "failed_request_not_terminal": (True, {"failed_request_not_terminal"}, None, "ok"),
    "legacy_value_perturbed": (False, set(), {CELL: "different", CELL_B: "different"}, "ok"),
    "cell_without_policy": (
        False, set(), {CELL: "equal", CELL_B: "equal", "fBTC_a30": "not_comparable"}, "ok"),
}


@pytest.mark.parametrize("name", sorted(DISCREPANCIES))
async def test_each_discrepancy_fails_the_run(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
    name: str,
) -> None:
    unknown, reasons, statuses, inventory = DISCREPANCIES[name]
    state = await build_cutover(bot_env, ledger_db, monkeypatch, tmp_path, unknown=unknown)
    overrides = _inject(state, name)
    code, rows = await run_comparison(state, **overrides)
    summary = rows[-1]
    assert code == 1, summary
    assert closure_reasons(rows) == reasons, [r for r in by_kind(rows, "closure") if r["violations"]]
    expected = statuses or {CELL: "equal", CELL_B: "equal"}
    assert {cell: row["status"] for cell, row in arms(rows).items()} == expected
    assert summary["inventory_status"] == inventory
    assert summary["arms"]["closure"]["passed"] is (not reasons)
    assert summary["arms"]["ledger_reader"]["passed"] is (statuses is None)
    if name == "cell_without_policy":
        assert arms(rows)["fBTC_a30"]["reason"] == "policy_missing"


async def test_the_window_is_bounded_by_the_injected_clock(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    """Q6: last observation's query start -> command end <= 300 s, by the command's clock."""
    state = await build_cutover(bot_env, ledger_db, monkeypatch, tmp_path, unknown=False)
    code, rows = await run_comparison(state, end=state.window_start + 300_000)
    assert code == 0, rows[-1]
    code, rows = await run_comparison(state, end=state.window_start + 300_001)
    assert code == 1 and rows[-1]["window"]["ok"] is False
    assert rows[-1]["arms"]["ledger_reader"]["passed"] and rows[-1]["arms"]["closure"]["passed"]
    code, rows = await run_comparison(state, as_of=state.as_of - 1)
    assert code == 3 and rows[-1]["reason"] == "as_of_mismatch"


async def _sync_recent_trade(env: BotEnv) -> None:
    """A trade sync after the final legacy snapshot: legacy can now attribute 8002 exactly."""
    async with env.factory.begin() as session:
        session.add(FundingTradeRow(
            exchange_account_id=SCOPE.exchange_account_id, trade_id=9002, deployment_environment="ci",
            symbol="fUST", mts_create=RECENT_OPENING, offer_id=7002, amount=Decimal("150"),
            rate=Decimal("0.0001"), period_days=2, maker=True))


async def test_f7_seeded_recent_fill_stays_multi_cell_as_a_declared_divergence(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    """F7 (Will 2026-10-05): legacy (i') attributes 8002 to a30 from the synced trade; the
    ledger keeps the seed's recent_fill cells {a30, b60}. b60 is ``declared`` with the group as
    evidence, not ``different``; the run passes."""
    state = await build_cutover(bot_env, ledger_db, monkeypatch, tmp_path, unknown=False,
                                before_seed=_sync_recent_trade)
    code, rows = await run_comparison(state)
    assert code == 0, rows[-1]
    by_cell = arms(rows)
    assert by_cell[CELL]["status"] == "equal"
    declared = by_cell[CELL_B]
    assert declared["status"] == "declared" and declared["reason"] == "f7_seeded_recent_fill_multi_cell"
    assert {d["path"] for d in declared["differences"]} >= {"snapshot.cell_exposure"}
    assert declared["declared"] == [{
        "credit": "credit:8002", "symbol": "fUST", "amount": "150", "period_days": 2,
        "mts_opening": RECENT_OPENING, "seed_basis": "recent_fill", "ledger_basis": "carry",
        "ledger_cells": [CELL, CELL_B], "legacy_cells": [CELL], "legacy_basis": "funding_trade",
    }]
    exposure = (Decimal(declared["ledger"]["snapshot.cell_exposure"])
                - Decimal(declared["legacy"]["snapshot.cell_exposure"]))
    assert exposure == Decimal("150")
    assert rows[-1]["arms"]["ledger_reader"]["counts"] == {"declared": 1, "equal": 1}


async def test_f7_divergence_with_any_other_difference_stays_different(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    state = await build_cutover(bot_env, ledger_db, monkeypatch, tmp_path, unknown=False,
                                before_seed=_sync_recent_trade)
    code, rows = await run_comparison(state, observation=_perturb_wallet(state.observation))
    assert code == 1
    assert arms(rows)[CELL_B]["status"] == "different"
