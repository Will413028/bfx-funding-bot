"""Helpers for the cutover comparison end to end (S1-4e, ``bot_e2e`` + ``seed_e2e`` composition).

The S1-4d closure (``test_ledger_seed_e2e.run_legacy``) is seeded by the real seed command; the
venue moves during the halt; a ledger process boots and takes the first real observation (it
stands in for the S1-5 cutover runner, which writes only that observation). The legacy REST
observation of the same venue state is fetched by the stopped legacy process's ``BootRecovery``
(HTTP only; the same fetch ``BootRecovery.run`` does, without its fence) and stamped with the
ledger runner's query times, so both arms see one observation at one as-of. The real
``capital_comparison`` command then runs as a ``NOINHERIT`` LOGIN that is a member of
``bfx_cutover_reader`` (the runbook shape; the command does ``SET LOCAL ROLE``).
"""
from __future__ import annotations

import io
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine

from bfx_funding_bot.apps import capital_comparison as command
from bfx_funding_bot.apps.capital_comparison_guard import READER_ROLE
from bfx_funding_bot.modules.execution.boot_recovery import (
    HISTORY_QUERY_MARGIN_MS,
    BootRecovery,
)
from bfx_funding_bot.modules.execution.capital_observed_baseline import (
    OBSERVATION_FORMAT,
    OBSERVATION_VERSION,
)
from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from bfx_funding_bot.modules.ledger.tables import LedgerObservationQueryRow, LedgerObservationRow

from .bot_e2e import CELL, SCOPE, BotEnv
from .seed_e2e import CELL_B, boot_as_epoch, flip_epoch, run_seed, seed_command
from .test_ledger_seed_e2e import RUNNER_AT, SEED_AT, Legacy, halt_moves, run_legacy

PASSWORD = "test-only"
WINDOW_MS = 300_000


@dataclass
class Cutover:
    env: BotEnv
    legacy: Legacy
    seed_lines: list[dict[str, Any]]
    observation: dict[str, Any]
    as_of: int
    window_start: int
    tmp_path: Path
    ledger_db: Engine
    login: str


def owner(ledger_db: Engine, *statements: str) -> None:
    """Owner statements on the test's own database clone."""
    with ledger_db.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)


def reader_login(ledger_db: Engine) -> str:
    """The runbook's attested LOGIN: NOINHERIT, member of the reader group only."""
    name = "cutover_" + uuid4().hex[:12]
    owner(ledger_db,
          f'CREATE ROLE "{name}" LOGIN PASSWORD \'{PASSWORD}\' NOSUPERUSER NOCREATEDB '
          "NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION",
          f'GRANT {READER_ROLE} TO "{name}"')
    return name


async def legacy_rest_observation(
    env: BotEnv, *, started: int, finished: int, confirmed: int,
    history_window: tuple[bool, int | None, int | None] | None = None,
) -> tuple[VenueSnapshotObserved, VenueSnapshotObserved]:
    """The stopped legacy process fetches the venue as ``BootRecovery.run`` does (no fence).

    ``history_window`` (complete, start, end) stamps the runner's offer-history request on the
    legacy coverage: the S1-5 runner makes one request and both arms read it (the fake venue
    serves the same rows for any window).
    """
    recovery: Any = env.daemons[0].periodic_reconcile._recovery
    while not isinstance(recovery, BootRecovery):  # timing and sink wrappers
        recovery = getattr(recovery, "_inner", None) or recovery._recovery
    history_start = await recovery._load_history_start_ms()
    offers = await recovery._fetch_offers(None)
    credits = await recovery._fetch_credits(None)
    wallets = await recovery._fetch_available_all()
    history = await recovery._fetch_history(
        start_ms=None if history_start is None else max(0, history_start - HISTORY_QUERY_MARGIN_MS),
        end_ms=started,
    )
    event = VenueSnapshotObserved(
        account_id=str(SCOPE.exchange_account_id), environment="ci",
        query_started_at_ms=started, query_finished_at_ms=finished,
        offers=tuple(recovery._offer_observation(o) for o in offers),
        credits=tuple(recovery._credit_observation(c) for c in credits),
        offer_history=tuple(recovery._offer_observation(o) for o in history.offers),
        wallet_available=wallets,
        coverage=SnapshotCoverage(
            active_offers_complete=True, active_credits_complete=True, wallets_complete=True,
            active_offer_pages=1, active_credit_pages=1, wallet_pages=1,
            offer_history_complete=(history.coverage.complete if history_window is None
                                    else history_window[0]),
            offer_history_pages=history.coverage.pages,
            offer_history_start_ms=(history.coverage.requested_start_ms if history_window is None
                                    else history_window[1]),
            offer_history_end_ms=(history.coverage.requested_end_ms if history_window is None
                                  else history_window[2]),
            offer_history_oldest_mts=history.coverage.oldest_mts_created,
            offer_history_newest_mts=history.coverage.newest_mts_created,
        ),
        occurred_at_ms=finished,
    )
    confirmation = replace(event, event_id=uuid4(), query_started_at_ms=finished,
                           query_finished_at_ms=confirmed, offer_history=())
    return event, confirmation


async def runner_observation(env: BotEnv) -> dict[str, Any]:
    """The scope's latest ledger observation: times, identity and digests (the runner's file
    names it; with no runner yet it is the seed's)."""
    async with env.factory() as session:
        row = (await session.execute(
            select(LedgerObservationQueryRow.started_at_ms, LedgerObservationRow.query_finished_at_ms,
                   LedgerObservationRow.confirmation_finished_at_ms, LedgerObservationRow.query_id,
                   LedgerObservationRow.id, LedgerObservationRow.first_digest,
                   LedgerObservationRow.confirmation_digest,
                   LedgerObservationRow.offer_history_complete,
                   LedgerObservationRow.history_requested_start_ms,
                   LedgerObservationRow.history_requested_end_ms)
            .join(LedgerObservationRow,
                  LedgerObservationRow.query_id == LedgerObservationQueryRow.query_id)
            .order_by(LedgerObservationQueryRow.query_revision.desc()).limit(1))).one()
    return {"started": int(row[0]), "finished": int(row[1]), "confirmed": int(row[2]),
            "history": (bool(row[7]), row[8], row[9]),
            "ledger": {"query_id": str(row[3]), "observation_id": str(row[4]),
                       "first_digest": row[5], "confirmation_digest": row[6]}}


def observation_file(event: VenueSnapshotObserved, confirmation: VenueSnapshotObserved,
                     ledger: dict[str, str]) -> dict[str, Any]:
    return {"format": OBSERVATION_FORMAT, "version": OBSERVATION_VERSION, "observations": [{
        "account_id": str(SCOPE.exchange_account_id), "environment": "ci",
        "event": serialize_event(event), "confirmation": serialize_event(confirmation),
        "ledger": ledger,
    }]}


async def build_cutover(
    env: BotEnv, ledger_db: Engine, monkeypatch: Any, tmp_path: Path, *, unknown: bool,
    before_seed: Callable[[BotEnv], Awaitable[None]] | None = None,
    at_capture: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
    runner: bool = True,
) -> Cutover:
    """Legacy closure -> seed -> halt moves -> ledger runner observation -> legacy observation.

    ``before_seed`` runs after the final legacy snapshot; ``at_capture`` right after the seed
    (the capture point, before any runner observation) with the seed's evidence lines.
    ``runner=False`` stops at the seed: the legacy observation is fetched at ``RUNNER_AT`` and the
    file names the seed's observation (the ledger is still on the seed basis).
    """
    legacy = await run_legacy(env, unknown=unknown)
    if before_seed is not None:
        await before_seed(env)
    code, seed_lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0, seed_lines
    if at_capture is not None:
        await at_capture(seed_lines)
    await flip_epoch(env, at=SEED_AT + 1_000)
    boot_as_epoch(env, monkeypatch)
    halt_moves(env)
    observed = await runner_observation(env)
    if runner:
        process = await env.build()
        await env.boot(process, RUNNER_AT)
        assert process.periodic_reconcile._non_accepted == 0
        observed = await runner_observation(env)
    else:
        observed.update(started=RUNNER_AT, finished=RUNNER_AT + 200, confirmed=RUNNER_AT + 400)
    started, finished, confirmed = observed["started"], observed["finished"], observed["confirmed"]
    event, confirmation = await legacy_rest_observation(
        env, started=started, finished=finished, confirmed=confirmed,
        history_window=observed["history"] if runner else None)
    # Q6 (R1-1): the window starts at the first observation's query start.
    return Cutover(env, legacy, seed_lines,
                   observation_file(event, confirmation, observed["ledger"]),
                   max(finished, confirmed), started, tmp_path, ledger_db,
                   reader_login(ledger_db))


def cells_file(tmp_path: Path, extra: tuple[str, ...] = ()) -> Path:
    path = tmp_path / "cutover-cells.yaml"
    path.write_text("cells:\n" + "".join(
        f"  - {{strategy: rate_percentile, symbol: {cell.split('_')[0]}, "
        f"period_agg: {cell.split('_')[1]}, timeframe: 1h, "
        "params: {percentile: 75, lookback_hours: 5}}\n"
        for cell in (CELL, CELL_B, *extra)))
    return path


def arguments(state: Cutover, *, observation: dict[str, Any] | None = None,
              seed_lines: list[dict[str, Any]] | None = None, as_of: int | None = None,
              extra_cells: tuple[str, ...] = ()) -> list[str]:
    tmp = state.tmp_path
    url = state.ledger_db.url.set(username=state.login, password=PASSWORD)
    dsn_file = tmp / "cutover.dsn"
    dsn_file.write_text(url.set(drivername="postgresql").render_as_string(hide_password=False) + "\n")
    os.chmod(dsn_file, 0o600)
    manifest = tmp / "cutover.json"
    manifest.write_text(json.dumps({
        "mode": "cutover", "host": url.host, "port": url.port, "database": url.database,
        "user": state.login, "run_id": "cutover-e2e",
        "now_ms": state.as_of if as_of is None else as_of,
        # fUSD has a (disabled) policy head and deliberately no cell.
        "policy_without_cell": [{"account_id": str(SCOPE.exchange_account_id),
                                 "environment": "ci", "symbol": "fUSD"}],
    }))
    observed = tmp / "observation.json"
    observed.write_text(json.dumps(state.observation if observation is None else observation))
    evidence = tmp / "seed-evidence.jsonl"
    evidence.write_text("".join(
        json.dumps(line) + "\n" for line in (state.seed_lines if seed_lines is None else seed_lines)))
    return [
        "--mode", "cutover", "--authorize-cutover-read", "--cutover-manifest", str(manifest),
        "--dsn-file", str(dsn_file), "--run-id", "cutover-e2e", "--code-revision", "e2e",
        "--scope", f"{SCOPE.exchange_account_id}:ci", "--cells", str(cells_file(tmp, extra_cells)),
        "--observation", str(observed), "--seed-evidence", str(evidence),
    ]  # fmt: skip


async def run_comparison(state: Cutover, *, end: int | None = None,
                         **overrides: Any) -> tuple[int, list[dict[str, Any]]]:
    """The real command; its clock (start and end of the window) is injected."""
    ticks = iter((state.as_of + 1_000, end if end is not None else state.window_start + 60_000))
    output = io.StringIO()
    code = await command.run(arguments(state, **overrides), output=output, clock=lambda: next(ticks))
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


def by_kind(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [row for row in rows if row["kind"] == kind]


def violations(rows: list[dict[str, Any]], check: str) -> list[str]:
    return [v["reason"] for row in by_kind(rows, "closure") if row["check"] == check
            for v in row["violations"]]


__all__ = [
    "PASSWORD", "WINDOW_MS", "Cutover", "arguments", "build_cutover", "by_kind", "cells_file",
    "legacy_rest_observation", "observation_file", "owner", "reader_login", "run_comparison",
    "runner_observation", "violations",
]
