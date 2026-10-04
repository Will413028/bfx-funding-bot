"""Helpers for the legacy -> ledger seed end-to-end tests (``bot_e2e`` composition).

A legacy bot process builds the closure on the HTTP-level fake venue; the test stops it, runs
the real seed command (``apps.ledger_seed.run``) as the table owner, flips the epoch as the
owner, and boots a ledger bot process on the same database and venue.
"""
from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from bfx_funding_bot.apps import bot, ledger_seed
from bfx_funding_bot.apps.read_models import select_read_models
from bfx_funding_bot.core.authority import read_authority
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.contracts import (
    ExecutionPolicy,
    GuardResult,
    ReadyToSubmit,
)
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitRejected,
)
from bfx_funding_bot.modules.ledger import CapitalAvailable
from bfx_funding_bot.modules.ledger.tables import (
    AcceptedCapitalBasisAttemptRow,
    AcceptedCapitalBasisCreditCellRow,
    AcceptedCapitalBasisCreditRow,
    AcceptedCapitalBasisRow,
    AcceptedCapitalBasisSymbolRow,
    CapitalAuthorityEpochRow,
    LedgerObservationQueryRow,
    LedgerObservationRow,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

from .bot_e2e import CELL, SCOPE, BotEnv, GateVenue

CELL_B = "fUST_b60"
RATE = "0.0001"
PERIOD = 2


def offer_row(offer_id: int, original: Decimal, remaining: Decimal, created: int, updated: int,
              status: str = "ACTIVE") -> list[Any]:
    """A Bitfinex funding offer row (lend side: negative amounts)."""
    return [offer_id, "fUST", created, updated, str(-remaining), str(-original), "LIMIT", None,
            None, 0, status, None, None, None, RATE, PERIOD, None]


def credit_row(credit_id: int, amount: Decimal, opening: int, created: int | None = None,
               status: str = "ACTIVE") -> list[Any]:
    """A Bitfinex funding credit (or loan) row; ``opening`` is MTS_OPENING."""
    at = opening if created is None else created
    return [credit_id, "fUST", 1, at, at, str(-amount), 0, status, "FIXED", None, None, RATE,
            PERIOD, opening, at, None]


def trade_row(trade_id: int, offer_id: int, amount: Decimal, at: int) -> list[Any]:
    return [trade_id, "fUST", at, offer_id, str(amount), RATE, PERIOD, 1]


def acked(venue_offer_id: int) -> GateVenue:
    return GateVenue(lambda _: SubmitAcknowledged(str(venue_offer_id)))


def rejected() -> GateVenue:
    return GateVenue(lambda _: SubmitRejected(reason="venue_rejected"))


async def decision(env: BotEnv, amount: Decimal, cell: str, now: int) -> tuple[str, UUID]:
    decision_id, correlation = str(uuid4()), uuid4()
    async with env.factory.begin() as session:
        session.add(ExecutionDecisionRow(
            decision_id=decision_id, account_id=str(SCOPE.exchange_account_id),
            exchange_account_id=SCOPE.exchange_account_id, deployment_environment="ci",
            reconcile_id="gate", cell_id=cell, symbol="fUST",
            signal_correlation_id=str(correlation), outcome="ready",
            signal_rate=Decimal(RATE), applied_rate=Decimal(RATE), amount_usdt=amount,
            duration_days=PERIOD, model_evidence={}, safety_result={}, execution_policy="gate",
            service_version="test", config_hash="test", occurred_at_ms=now, recorded_at_ms=now,
        ))
    return decision_id, correlation


async def ready(env: BotEnv, daemon: Any, amount: Decimal, cell: str = CELL) -> ReadyToSubmit:
    """A submit the gate takes now, for ``cell`` (``BotEnv.ready`` with a cell)."""
    from tests.external.bitfinex.test_funding_rules import evidence
    from tests.modules.execution.deployment.test_reconciler import _valid_snapshot

    now = env.clock()
    view = await env.capital(daemon)
    assert isinstance(view, CapitalAvailable), view
    decision_id, correlation = await decision(env, amount, cell, now)
    return ReadyToSubmit(
        decision=DecisionPayload(
            decision_outcome=DecisionOutcome.POST, signal_correlation_id=correlation,
            offer_rate=Decimal(RATE), offer_amount_usdt=amount, offer_duration_days=PERIOD,
            symbol="fUST"),
        decision_id=decision_id, policy=ExecutionPolicy.BOOK_GUARDED,
        market_snapshot_id="book", model_version=None, evidence={},
        safety=GuardResult(True, "test"), capital_view=view,
        market_snapshot=replace(_valid_snapshot(), snapshot_id="book", captured_at_ms=now,
                                received_at_ms=now, max_age_ms=30_000),
        funding_amount_evidence=evidence(now=now),
    )


async def submit(env: BotEnv, daemon: Any, amount: Decimal, venue: GateVenue, *,
                 cell: str = CELL) -> Any:
    result = await env.submit(daemon, await ready(env, daemon, amount, cell), venue)
    assert venue.calls == 1, result
    return result


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


async def flip_epoch(env: BotEnv, *, at: int) -> None:
    """The owner appends the ``ledger`` epoch (the S1-7 switch step)."""
    async with env.factory.begin() as session:
        latest = await session.scalar(
            select(CapitalAuthorityEpochRow.epoch_seq).order_by(
                CapitalAuthorityEpochRow.epoch_seq.desc()).limit(1))
        session.add(CapitalAuthorityEpochRow(
            epoch_seq=int(latest or 0) + 1, authority="ledger", set_at_ms=at,
            actor="seed-e2e", reason="authority switch after seed", evidence=None))


def boot_as_epoch(env: BotEnv, monkeypatch: Any) -> None:
    """Later processes read the real epoch; only Bitfinex's ``legacy``-only support is lifted
    (production refuses a ledger Bitfinex boot until S1-7, ``test_daemon_authority_wiring``)."""
    async def epoch(session: Any, *, supported: object) -> str:
        return await read_authority(session, supported=frozenset({"legacy", "ledger"}))

    monkeypatch.setattr(bot, "read_authority", epoch)
    env.authority = "ledger"
    env.reads = select_read_models("ledger")


async def latest_bases(env: BotEnv) -> list[AcceptedCapitalBasisRow]:
    """Every accepted basis of the scope, newest query first."""
    async with env.factory() as session:
        return list(await session.scalars(
            select(AcceptedCapitalBasisRow)
            .join(LedgerObservationRow,
                  LedgerObservationRow.id == AcceptedCapitalBasisRow.observation_id)
            .join(LedgerObservationQueryRow,
                  LedgerObservationQueryRow.query_id == LedgerObservationRow.query_id)
            .order_by(LedgerObservationQueryRow.query_revision.desc())))


async def basis_view(env: BotEnv, basis_id: UUID) -> dict[str, Any]:
    async with env.factory() as session:
        symbols = {row.symbol: row for row in await session.scalars(
            select(AcceptedCapitalBasisSymbolRow).where(
                AcceptedCapitalBasisSymbolRow.basis_id == basis_id))}
        credits = list(await session.scalars(select(AcceptedCapitalBasisCreditRow).where(
            AcceptedCapitalBasisCreditRow.basis_id == basis_id)))
        cells: dict[tuple[str, str], set[str]] = {}
        for row in await session.scalars(select(AcceptedCapitalBasisCreditCellRow).where(
                AcceptedCapitalBasisCreditCellRow.basis_id == basis_id)):
            cells.setdefault((row.source_kind, row.venue_credit_id), set()).add(row.cell_id)
        attempts = {row.attempt_id: row.classification for row in await session.scalars(
            select(AcceptedCapitalBasisAttemptRow).where(
                AcceptedCapitalBasisAttemptRow.basis_id == basis_id))}
    groups = {
        (c.source_kind, c.venue_credit_id): (c.period_days, c.mts_opening, c.attribution_basis,
                                             frozenset(cells.get((c.source_kind, c.venue_credit_id), ())))
        for c in credits
    }
    return {"symbols": symbols, "groups": groups, "attempts": attempts}


async def table_count(env: BotEnv, name: str) -> int:
    async with env.factory() as session:
        return int(await session.scalar(text(f"SELECT count(*) FROM {name}")) or 0)


__all__ = [
    "CELL_B", "acked", "basis_view", "boot_as_epoch", "credit_row", "decision", "flip_epoch",
    "latest_bases", "offer_row", "ready", "rejected", "run_seed", "seed_command", "submit",
    "table_count", "trade_row",
]
