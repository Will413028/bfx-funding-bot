"""Soak report of the simulated venue: the ADR D3 bar plus the decision-C activity floor.

Reads the simulation database (read-only; run it as a one-shot of the deployed image with
``sim.env``, docs/runbooks/simulation-soak.md) over the window ``--since`` .. ``--until`` and
prints PASS / FAIL / UNAVAILABLE per criterion with the numbers behind it. Exit code 0 when
every criterion passes, 1 when any fails, 3 when none fails but one could not be read (an
unreadable source is never read as zero), 2 for a refused run (not a ``shadow`` database,
unusable arguments).

    python -m scripts.sim_soak_report --exchange-account-id UUID \\
      --since 2026-10-05T00:00:00Z --until 2026-10-08T00:00:00Z --metrics gen1.metrics

``DATABASE_URL`` must be supplied explicitly (this command does not load .env).

Process-local counters (internal failures, unexpected requests, ...) reset on every restart, so
the orchestrator saves the raw ``/metrics`` text of each process generation before stopping it
and passes every file once::

    --metrics gen1-<rev>.metrics --metrics gen2-<rev>.metrics

A family that is present in a file but has no sample counts as 0 (the process ran and never
incremented it); a family that is missing, or a file that cannot be parsed, makes the criterion
UNAVAILABLE, never 0.

Injected versus organic: every injected venue fault is a ``fault_injected`` row in
``sim_venue_event`` (durable across restarts). An UNKNOWN submit attempt is injected when an
injection of a submit fault lies inside its ``started_at_ms .. completed_at_ms`` interval
(the bot sends one authenticated request at a time, and the venue sees no client order id);
a quarantine is injected when it was opened by such an attempt. Everything else is organic.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch, read_database_realm
from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory

# -- the bar -----------------------------------------------------------------------------
# docs/adr/2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md, D3 and its
# 2026-10-04 amendment. Fixed before the soak starts; a change is an ADR amendment.
MIN_WINDOW_HOURS = 72
MIN_DEPLOY_RESTARTS = 2
MIN_OPERATOR_KILLS = 1
MIN_ACCEPTED_CYCLE_RATIO = Decimal("0.99")
MIN_TRADING_HOURS_AFTER_RESUME = 24  # amendment (B): the kill is followed by real trading
# Non-vacuity floor (amendment, decision C): a halted or idle bot satisfies every safety
# criterion, so the window must also show this much activity.
FLOOR_ACKED_SUBMITS = 50
FLOOR_FILLS = 10
FLOOR_CANCELS = 10  # cancels and reprices: every cancelled offer of the venue's own log
FLOOR_CREDITS_CLOSED_BY_EXPIRY = 1
FLOOR_INTEREST_PAYMENTS_PER_DAY = 1  # per UTC calendar day lying wholly inside the window

# Metric families (prometheus_client names, without the ``_total`` of their samples).
FAMILY_INTERNAL_FAILURES = "bfx_sim_venue_internal_failures"
FAMILY_UNEXPECTED_REQUESTS = "bfx_sim_venue_unexpected_requests"
FAMILY_FEED_FAILURES = "bfx_sim_venue_feed_failures"
FAMILY_FEED_STREAM = "bfx_sim_venue_feed_stream"
FAMILY_WATERMARK_LAG = "bfx_sim_venue_feed_watermark_lag_seconds"

# An injection is matched to an attempt within its interval, widened by the clock rounding of
# two processes that share one clock.
INJECTION_MATCH_SLACK_MS = 5_000
UNKNOWN_FAULT_KINDS = frozenset({
    "unknown_5xx_error", "unknown_placed_lost", "unknown_not_placed_lost"})
UNIDENTIFIED_VERSIONS = frozenset({"", "unidentified", "unknown"})
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

Status = Literal["PASS", "FAIL", "UNAVAILABLE"]


class ReportRefused(RuntimeError):  # noqa: N818 - a refusal, carrying its reason code
    """The report is not run on a database or with arguments it can speak for."""


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    rule: str
    status: Status
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Window:
    since_ms: int
    until_ms: int

    def __post_init__(self) -> None:
        if self.until_ms <= self.since_ms or self.since_ms < 0:
            raise ReportRefused("window_invalid")

    @property
    def hours(self) -> float:
        return (self.until_ms - self.since_ms) / HOUR_MS


@dataclass(frozen=True, slots=True)
class Scope:
    account: UUID
    realm: str

    @property
    def params(self) -> dict[str, Any]:
        return {"account": self.account, "account_text": str(self.account), "realm": self.realm}


def _verdict(ok: bool) -> Status:
    return "PASS" if ok else "FAIL"


# -- reading -----------------------------------------------------------------------------


async def _rows(
    session: AsyncSession, sql: str | TextClause, params: Mapping[str, Any],
) -> list[Any]:
    statement = text(sql) if isinstance(sql, str) else sql
    return list((await session.execute(statement, dict(params))).all())


async def _scalar(session: AsyncSession, sql: str, params: Mapping[str, Any]) -> Any:
    return await session.scalar(text(sql), dict(params))


async def _injections(
    session: AsyncSession, scope: Scope, window: Window,
) -> list[dict[str, Any]]:
    """Injection events whose time lies in the window widened by the match slack."""
    rows = await _rows(session, """
        SELECT payload->'data' AS data FROM sim_venue_event
        WHERE exchange_account_id = :account_text AND deployment_environment = :realm
          AND event_type = 'fault_injected'
          AND (payload->'data'->>'mts')::bigint BETWEEN :lo AND :hi
        ORDER BY seq""", {**scope.params, "lo": window.since_ms - INJECTION_MATCH_SLACK_MS,
                          "hi": window.until_ms + INJECTION_MATCH_SLACK_MS})
    return [dict(r.data) for r in rows]


def match_injected_attempts(
    attempts: Sequence[tuple[UUID, int, int]], injections: Sequence[Mapping[str, Any]],
) -> set[UUID]:
    """Attempts (id, started, completed) that an unused submit injection of an UNKNOWN fault
    falls inside of; each injection explains at most one attempt."""
    pool = sorted(
        int(i["mts"]) for i in injections
        if i["target"] == "submit" and i["fault_kind"] in UNKNOWN_FAULT_KINDS)
    injected: set[UUID] = set()
    for attempt_id, started, completed in sorted(attempts, key=lambda a: a[1]):
        for index, mts in enumerate(pool):
            if started - INJECTION_MATCH_SLACK_MS <= mts <= completed + INJECTION_MATCH_SLACK_MS:
                injected.add(attempt_id)
                del pool[index]
                break
    return injected


# -- the criteria ------------------------------------------------------------------------


def window_criterion(window: Window) -> Criterion:
    return Criterion(
        "d3.window_hours", f">= {MIN_WINDOW_HOURS} h", _verdict(window.hours >= MIN_WINDOW_HOURS),
        {"hours": round(window.hours, 2), "since_ms": window.since_ms, "until_ms": window.until_ms})


async def deploy_restarts(session: AsyncSession, scope: Scope, window: Window) -> Criterion:
    rows = await _rows(session, """
        SELECT service_version, min(occurred_at_ms) AS first_ms, max(occurred_at_ms) AS last_ms,
               count(*) AS decisions
        FROM execution_decisions
        WHERE account_id = :account_text AND deployment_environment = :realm
          AND occurred_at_ms BETWEEN :t0 AND :t1
        GROUP BY service_version ORDER BY min(occurred_at_ms)""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    revisions = [
        {"service_version": r.service_version, "first_ms": r.first_ms, "last_ms": r.last_ms,
         "decisions": r.decisions} for r in rows]
    unidentified = [r["service_version"] for r in revisions
                    if r["service_version"].strip().lower() in UNIDENTIFIED_VERSIONS]
    restarts = max(len(revisions) - 1, 0)
    # An unidentified revision means the container ran without BFX_SOURCE_REVISION: the
    # restart count cannot be trusted, which is a failure and never a free pass.
    ok = restarts >= MIN_DEPLOY_RESTARTS and not unidentified
    return Criterion(
        "d3.deploy_restarts", f">= {MIN_DEPLOY_RESTARTS} on a new revision, none unidentified",
        _verdict(ok),
        {"deploy_restarts": restarts, "revisions": revisions, "unidentified": unidentified})


async def kill_and_resume(session: AsyncSession, scope: Scope, window: Window) -> list[Criterion]:
    halts = await _rows(session, """
        SELECT s.id, s.actor, s.created_at_ms,
               count(a.id) FILTER (WHERE a.phase = 'acknowledged') AS acknowledged
        FROM trading_state s
        LEFT JOIN funding_cancel_all_audit a ON a.trading_state_id = s.id
        WHERE s.exchange_account_id = :account AND s.deployment_environment = :realm
          AND s.state = 'HALTED' AND s.cause = 'operator'
          AND s.created_at_ms BETWEEN :t0 AND :t1
        GROUP BY s.id, s.actor, s.created_at_ms ORDER BY s.id""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    kills = [h for h in halts if h.acknowledged > 0]
    kill_criterion = Criterion(
        "d3.operator_kill", f">= {MIN_OPERATOR_KILLS} operator HALT with an acknowledged cancel-all",
        _verdict(len(kills) >= MIN_OPERATOR_KILLS),
        {"operator_halts": len(halts), "with_acknowledged_cancel_all": len(kills),
         "actors": sorted({h.actor for h in halts})})
    if not kills:
        return [kill_criterion, Criterion(
            "amendment.trading_after_resume",
            f">= {MIN_TRADING_HOURS_AFTER_RESUME} h of trading after the resume",
            "FAIL", {"reason": "no kill to resume from"})]
    last_kill = kills[-1]
    resume = await _rows(session, """
        SELECT id, actor, created_at_ms FROM trading_state
        WHERE exchange_account_id = :account AND deployment_environment = :realm
          AND state = 'ACTIVE' AND id > :kill AND created_at_ms <= :t1
        ORDER BY id LIMIT 1""", {**scope.params, "kill": last_kill.id, "t1": window.until_ms})
    if not resume:
        return [kill_criterion, Criterion(
            "amendment.trading_after_resume",
            f">= {MIN_TRADING_HOURS_AFTER_RESUME} h of trading after the resume",
            "FAIL", {"reason": "HALTED at the end of the window", "kill_id": last_kill.id})]
    resumed_ms = resume[0].created_at_ms
    submits = await _scalar(session, """
        SELECT count(*) FROM transport_outcome_journal t
        JOIN submission_attempt_journal a ON a.attempt_id = t.attempt_id
        WHERE a.exchange_account_id = :account AND a.deployment_environment = :realm
          AND t.kind = 'ack' AND t.completed_at_ms > :resumed AND t.completed_at_ms <= :t1""",
        {**scope.params, "resumed": resumed_ms, "t1": window.until_ms})
    hours = (window.until_ms - resumed_ms) / HOUR_MS
    return [kill_criterion, Criterion(
        "amendment.trading_after_resume",
        f">= {MIN_TRADING_HOURS_AFTER_RESUME} h after the resume, with acked submits in it",
        _verdict(hours >= MIN_TRADING_HOURS_AFTER_RESUME and submits > 0),
        {"hours_after_resume": round(hours, 2), "acked_submits_after_resume": submits,
         "resume_actor": resume[0].actor, "resumed_at_ms": resumed_ms})]


async def conservation(session: AsyncSession, scope: Scope, window: Window) -> list[Criterion]:
    rows = await _rows(session, """
        SELECT s.conservation, count(*) AS symbols, coalesce(sum(s.fill_conflicts), 0) AS conflicts
        FROM accepted_capital_basis_symbol s
        JOIN accepted_capital_basis b ON b.id = s.basis_id
        WHERE b.exchange_account_id = :account AND b.deployment_environment = :realm
          AND b.accepted_at_ms BETWEEN :t0 AND :t1
        GROUP BY s.conservation""", {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    by_verdict = {r.conservation: r.symbols for r in rows}
    total = sum(by_verdict.values())
    return [
        Criterion("d3.unexplained_lending", "== 0 (and at least one accepted basis)",
                  _verdict(total > 0 and by_verdict.get("unexplained_lending", 0) == 0),
                  {"unexplained_lending": by_verdict.get("unexplained_lending", 0),
                   "basis_symbols": total, "by_verdict": by_verdict}),
        Criterion("extra.foreign_lending", "== 0 (the simulation has no foreign actor)",
                  _verdict(total > 0 and by_verdict.get("foreign_lending", 0) == 0),
                  {"foreign_lending": by_verdict.get("foreign_lending", 0), "basis_symbols": total}),
    ]


async def accepted_cycles(session: AsyncSession, scope: Scope, window: Window) -> Criterion:
    row = (await _rows(session, """
        SELECT count(*) AS cycles, count(o.id) AS accepted
        FROM ledger_observation_query q
        LEFT JOIN ledger_observation o ON o.query_id = q.query_id AND o.accepted
        WHERE q.exchange_account_id = :account AND q.deployment_environment = :realm
          AND q.started_at_ms BETWEEN :t0 AND :t1""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms}))[0]
    # A query without an observation (a crash or restart mid-cycle) counts as not accepted.
    ratio = Decimal(row.accepted) / Decimal(row.cycles) if row.cycles else Decimal(0)
    return Criterion(
        "d3.accepted_cycle_ratio", f">= {MIN_ACCEPTED_CYCLE_RATIO}",
        _verdict(row.cycles > 0 and ratio >= MIN_ACCEPTED_CYCLE_RATIO),
        {"queries": row.cycles, "accepted": row.accepted, "ratio": f"{ratio:.4f}"})


async def uncertainty(
    session: AsyncSession, scope: Scope, window: Window,
) -> list[Criterion]:
    """Organic versus injected UNKNOWN / quarantine, and whether the injected ones closed."""
    attempts = [(r.attempt_id, r.started, r.completed) for r in await _rows(session, """
        SELECT a.attempt_id, a.started_at_ms AS started, t.completed_at_ms AS completed
        FROM transport_outcome_journal t
        JOIN submission_attempt_journal a ON a.attempt_id = t.attempt_id
        WHERE a.exchange_account_id = :account AND a.deployment_environment = :realm
          AND t.kind = 'unknown' AND t.completed_at_ms BETWEEN :t0 AND :t1""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})]
    quarantines = await _rows(session, """
        SELECT quarantine_id, source_attempt_id FROM quarantine_opening
        WHERE exchange_account_id = :account AND deployment_environment = :realm
          AND opened_at_ms BETWEEN :t0 AND :t1""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    injections = await _injections(session, scope, window)
    in_window = [i for i in injections
                 if window.since_ms <= int(i["mts"]) <= window.until_ms]
    submit_injections = [i for i in in_window
                         if i["target"] == "submit" and i["fault_kind"] in UNKNOWN_FAULT_KINDS]
    injected = match_injected_attempts(attempts, injections)
    injected_quarantines = [q for q in quarantines if q.source_attempt_id in injected]
    organic_unknown = len(attempts) - len(injected)
    organic_quarantine = len(quarantines) - len(injected_quarantines)
    by_kind: dict[str, int] = {}
    for item in in_window:
        by_kind[item["fault_kind"]] = by_kind.get(item["fault_kind"], 0) + 1

    organic = Criterion(
        "d3.organic_quarantine_unknown", "== 0 organic UNKNOWN attempts and quarantines",
        _verdict(organic_unknown == 0 and organic_quarantine == 0),
        {"organic_unknown": organic_unknown, "organic_quarantine": organic_quarantine,
         "injected_unknown": len(injected), "injected_quarantine": len(injected_quarantines),
         "unknown_attempts": len(attempts), "quarantines": len(quarantines)})

    open_attempts = len(injected)
    open_quarantines = len(injected_quarantines)
    manual = 0
    if injected:
        resolved = await _rows(session, text_in("""
            SELECT attempt_id, action FROM execution_resolution_journal
            WHERE attempt_id IN :ids"""), {"ids": sorted(injected)})
        closed = {r.attempt_id for r in resolved if r.action != "manual"}
        manual += sum(1 for r in resolved if r.action == "manual")
        open_attempts = len(injected - closed)
    if injected_quarantines:
        resolved_q = await _rows(session, text_in("""
            SELECT quarantine_id, action FROM execution_resolution_journal
            WHERE quarantine_id IN :ids"""), {"ids": [q.quarantine_id for q in injected_quarantines]})
        closed_q = {r.quarantine_id for r in resolved_q if r.action != "manual"}
        manual += sum(1 for r in resolved_q if r.action == "manual")
        open_quarantines = len({q.quarantine_id for q in injected_quarantines} - closed_q)
    # No injection at all, or none that became an UNKNOWN attempt, proves nothing: that is
    # a failure of the criterion, not a vacuous pass.
    auto_closed = Criterion(
        "d3.injected_unknown_auto_closed",
        "every injected UNKNOWN (and its quarantine) closed automatically, at least one injected",
        _verdict(bool(injected) and open_attempts == 0 and open_quarantines == 0 and manual == 0),
        {"injected_submit_faults": len(submit_injections), "injected_unknown": len(injected),
         "open_attempts": open_attempts, "open_quarantines": open_quarantines,
         "closed_manually": manual, "injections_by_kind": by_kind})
    return [organic, auto_closed]


def text_in(sql: str) -> TextClause:
    return text(sql).bindparams(bindparam("ids", expanding=True))


async def activity_floor(session: AsyncSession, scope: Scope, window: Window) -> list[Criterion]:
    acked = await _scalar(session, """
        SELECT count(*) FROM transport_outcome_journal t
        JOIN submission_attempt_journal a ON a.attempt_id = t.attempt_id
        WHERE a.exchange_account_id = :account AND a.deployment_environment = :realm
          AND t.kind = 'ack' AND t.completed_at_ms BETWEEN :t0 AND :t1""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    venue = {r.event_type: r.n for r in await _rows(session, """
        SELECT event_type, count(*) AS n FROM sim_venue_event
        WHERE exchange_account_id = :account_text AND deployment_environment = :realm
          AND event_type IN ('offer_filled', 'offer_canceled')
          AND (payload->'data'->>'mts')::bigint BETWEEN :t0 AND :t1
        GROUP BY event_type""", {**scope.params, "t0": window.since_ms, "t1": window.until_ms})}
    expired = await _scalar(session, """
        SELECT count(*) FROM sim_venue_event
        WHERE exchange_account_id = :account_text AND deployment_environment = :realm
          AND event_type = 'credit_closed' AND payload->'data'->>'reason' = 'expired'
          AND (payload->'data'->>'mts')::bigint BETWEEN :t0 AND :t1""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    interest_times = [int(r.mts) for r in await _rows(session, """
        SELECT (payload->'data'->>'mts')::bigint AS mts FROM sim_venue_event
        WHERE exchange_account_id = :account_text AND deployment_environment = :realm
          AND event_type = 'interest_paid'
          AND (payload->'data'->>'mts')::bigint BETWEEN :t0 AND :t1""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})]
    first_day = -(-window.since_ms // DAY_MS) * DAY_MS
    days = list(range(first_day, window.until_ms - DAY_MS + 1, DAY_MS))
    per_day = {
        datetime.fromtimestamp(day / 1000, UTC).strftime("%Y-%m-%d"):
            sum(1 for t in interest_times if day <= t < day + DAY_MS)
        for day in days}
    fills, cancels = venue.get("offer_filled", 0), venue.get("offer_canceled", 0)
    return [
        Criterion("floor.acked_submits", f">= {FLOOR_ACKED_SUBMITS}",
                  _verdict(acked >= FLOOR_ACKED_SUBMITS), {"acked_submits": acked}),
        Criterion("floor.fills", f">= {FLOOR_FILLS}", _verdict(fills >= FLOOR_FILLS),
                  {"offer_filled": fills}),
        Criterion("floor.cancels", f">= {FLOOR_CANCELS} cancels or reprices",
                  _verdict(cancels >= FLOOR_CANCELS), {"offer_canceled": cancels}),
        Criterion("floor.credits_closed_by_expiry", f">= {FLOOR_CREDITS_CLOSED_BY_EXPIRY}",
                  _verdict(expired >= FLOOR_CREDITS_CLOSED_BY_EXPIRY),
                  {"credit_closed_expired": expired}),
        Criterion("floor.interest_per_day",
                  f">= {FLOOR_INTEREST_PAYMENTS_PER_DAY} payment on each of >= 1 whole UTC days",
                  _verdict(bool(per_day) and all(
                      n >= FLOOR_INTEREST_PAYMENTS_PER_DAY for n in per_day.values())),
                  {"whole_days": len(per_day), "payments_by_day": per_day}),
    ]


def _read_generation(path: Path) -> dict[str, list[Any]]:
    """Family name -> samples of one saved ``/metrics`` scrape (families without samples kept)."""
    from prometheus_client.parser import text_string_to_metric_families

    families: dict[str, list[Any]] = {}
    for family in text_string_to_metric_families(path.read_text()):
        families.setdefault(family.name, []).extend(family.samples)
    return families


def process_counters(metrics_files: Sequence[Path]) -> list[Criterion]:
    """The per-process simulator counters, summed over every archived process generation."""
    rule = "== 0 over every process generation"
    ids = (("extra.internal_failures", FAMILY_INTERNAL_FAILURES),
           ("extra.unexpected_requests", FAMILY_UNEXPECTED_REQUESTS))
    if not metrics_files:
        return [Criterion(i, rule, "UNAVAILABLE", {"reason": "no metrics files"}) for i, _ in ids]
    generations: list[dict[str, list[Any]]] = []
    try:
        for path in metrics_files:
            generations.append(_read_generation(path))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return [Criterion(i, rule, "UNAVAILABLE",
                          {"reason": f"metrics file unreadable: {type(exc).__name__}"})
                for i, _ in ids]

    def total(family: str, **labels: str) -> float:
        return float(sum(
            sample.value for generation in generations for sample in generation[family]
            if sample.name.endswith("_total")
            and all(sample.labels.get(k) == v for k, v in labels.items())))

    out: list[Criterion] = []
    for criterion_id, family in ids:
        missing = [str(p) for p, g in zip(metrics_files, generations, strict=True)
                   if family not in g]
        if missing:
            out.append(Criterion(criterion_id, rule, "UNAVAILABLE",
                                 {"reason": f"{family} missing", "files": missing}))
            continue
        evidence: dict[str, Any] = {"total": total(family), "generations": len(generations)}
        if all(FAMILY_FEED_FAILURES in g for g in generations):
            evidence["info.feed_failures"] = total(FAMILY_FEED_FAILURES)
        if all(FAMILY_FEED_STREAM in g for g in generations):
            evidence["info.trades_ws_disconnects"] = total(FAMILY_FEED_STREAM, event="disconnected")
        lags = [s.value for g in generations for s in g.get(FAMILY_WATERMARK_LAG, [])]
        if lags:
            evidence["info.watermark_lag_seconds_at_last_scrape_max"] = max(lags)
        out.append(Criterion(criterion_id, rule, _verdict(evidence["total"] == 0), evidence))
    return out


# -- the report --------------------------------------------------------------------------


async def build_report(
    session: AsyncSession, scope: Scope, window: Window, *,
    metrics_files: Sequence[Path] = (),
) -> list[Criterion]:
    criteria = [window_criterion(window), await deploy_restarts(session, scope, window)]
    criteria += await kill_and_resume(session, scope, window)
    criteria += await conservation(session, scope, window)
    criteria.append(await accepted_cycles(session, scope, window))
    criteria += await uncertainty(session, scope, window)
    criteria += await activity_floor(session, scope, window)
    criteria += process_counters(metrics_files)
    return criteria


def exit_code(criteria: Sequence[Criterion]) -> int:
    if any(c.status == "FAIL" for c in criteria):
        return 1
    if any(c.status == "UNAVAILABLE" for c in criteria):
        return 3
    return 0


def render(criteria: Sequence[Criterion], *, event_count: int | None = None) -> str:
    lines = [
        f"{c.status:<11} {c.id:<36} {c.rule}  |  "
        + " ".join(f"{k}={json.dumps(v, sort_keys=True, default=str)}"
                   for k, v in c.evidence.items())
        for c in criteria]
    failed = [c.id for c in criteria if c.status == "FAIL"]
    unavailable = [c.id for c in criteria if c.status == "UNAVAILABLE"]
    lines.append("")
    if event_count is not None:
        lines.append(f"INFO sim_venue_event rows (boot replay grows with this): {event_count}")
    lines.append("RESULT " + ("FAIL " + ",".join(failed) if failed else
                              "UNAVAILABLE " + ",".join(unavailable) if unavailable else "PASS"))
    return "\n".join(lines)


async def run(
    *, database_url: str, account: UUID, window: Window, metrics_files: Sequence[Path] = (),
    realm: str = "shadow",
) -> tuple[list[Criterion], int]:
    engine = make_async_engine_from_url(database_url)
    try:
        async with make_session_factory(engine)() as session:
            stamp = await read_database_realm(session)
            if stamp != realm:
                raise ReportRefused(f"database_realm_not_simulation stamp={stamp}")
            criteria = await build_report(
                session, Scope(account, realm), window, metrics_files=metrics_files)
            events = await _scalar(session, """
                SELECT count(*) FROM sim_venue_event
                WHERE exchange_account_id = :account_text AND deployment_environment = :realm""",
                Scope(account, realm).params)
        return criteria, int(events)
    finally:
        await engine.dispose()


def _instant(raw: str) -> int:
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("expected ISO-8601, e.g. 2026-10-05T00:00:00Z") from None
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError("the instant needs a UTC offset (Z)")
    return int(moment.timestamp() * 1000)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exchange-account-id", type=UUID, required=True)
    parser.add_argument("--since", type=_instant, required=True, help="window start (UTC)")
    parser.add_argument("--until", type=_instant, required=True, help="window end (UTC)")
    parser.add_argument("--metrics", type=Path, action="append", default=[],
                        help="saved /metrics text of one process generation (repeat per file)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    logging.disable(sys.maxsize)
    try:
        criteria, events = asyncio.run(run(
            database_url=os.environ["DATABASE_URL"], account=args.exchange_account_id,
            window=Window(args.since, args.until), metrics_files=args.metrics))
    except (ReportRefused, DatabaseRealmMismatch) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Never expose a connection URL or DB bind values.
        print('{"status":"refused","reason":"report_failed"}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "criteria": [{"id": c.id, "rule": c.rule, "status": c.status,
                          "evidence": dict(c.evidence)} for c in criteria],
            "sim_venue_event_rows": events, "exit_code": exit_code(criteria)},
            sort_keys=True, indent=2, default=str))
    else:
        print(render(criteria, event_count=events))
    return exit_code(criteria)


if __name__ == "__main__":
    raise SystemExit(main())
