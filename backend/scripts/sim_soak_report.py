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

What the simulator itself did wrong is read from the database: internal failures and
unrouted requests are ``internal_failure_recorded`` / ``unexpected_request_recorded`` rows of
``sim_venue_event`` (durable across restarts and crashes; bounded per process).

The feed counters (trades-WS disconnects, truncated or unrecoverable backfills) live only in a
process, so the orchestrator saves the raw ``/metrics`` text of each process generation before
stopping it and passes every file once::

    --metrics gen1-<rev>.metrics --metrics gen2-<rev>.metrics

A family that is present in a file but has no sample counts as 0 (the process ran and never
incremented it); a family that is missing, a generation without a snapshot, or a file that
cannot be parsed makes only the criterion that needs it UNAVAILABLE, never 0.

Injected versus organic: every injected venue fault is a ``fault_injected`` row in
``sim_venue_event`` (durable across restarts). A submit injection records what the request
asked for; an UNKNOWN submit attempt is injected when an unused injection with the same
symbol, amount, rate and period lies inside its ``started_at_ms .. completed_at_ms`` interval.
A quarantine is injected when such an attempt opened it. Everything else is organic. A ledger
cycle whose time span holds a history injection is reported apart and left out of the gated
accepted-cycle ratio.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NamedTuple
from uuid import UUID

from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch, read_database_realm
from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory

# -- the bar -----------------------------------------------------------------------------
# docs/adr/2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md, D3 and its
# 2026-10-04 amendment. Fixed before the soak starts; a change is an ADR amendment.
# 2026-10-05 amendment (Will): the bar is 24 h, the switch is no longer gated on decision C.
MIN_WINDOW_HOURS = 24
MIN_DEPLOY_RESTARTS = 1
MIN_OPERATOR_KILLS = 1
KILL_WINDOW_START_HOUR = 6  # the kill lands this many hours after the window opens ...
KILL_WINDOW_END_HOUR = 10  # ... and no later than this, so >= 8 h of trading can follow it
MIN_ACCEPTED_CYCLE_RATIO = Decimal("0.99")
MIN_TRADING_HOURS_AFTER_RESUME = 8  # amendment (B): the kill is followed by real trading
# Non-vacuity floor (amendment, decision C): a halted or idle bot satisfies every safety
# criterion, so the window must also show this much activity.
# Floors are prod's last-7-day rates x 0.5 per 24 h (measured 2026-10-05: 10 acked submits, 10
# fills, 10 credit closures, ~0 cancels in 7 days). Cancels have no floor and expiry settlement
# is reported only (CI oracle covers it); the window needs >= 1 interest payment.
FLOOR_ACKED_SUBMITS = 1
FLOOR_FILLS = 1
FLOOR_INTEREST_PAYMENTS = 1

# Metric families (prometheus_client names, without the ``_total`` of their samples).
FAMILY_FEED_FAILURES = "bfx_sim_venue_feed_failures"
FAMILY_FEED_STREAM = "bfx_sim_venue_feed_stream"
FAMILY_WATERMARK_LAG = "bfx_sim_venue_feed_watermark_lag_seconds"

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


class Attempt(NamedTuple):
    """An UNKNOWN submit attempt: its interval and what it asked for."""

    attempt_id: UUID
    started: int
    completed: int
    symbol: str
    amount: Decimal | None
    rate: Decimal | None
    period: int | None


def _decimal(value: Any) -> Decimal | None:
    """A venue-event decimal (``{"$dec": "150"}``) or a journal number; None when absent/odd."""
    if isinstance(value, Mapping):
        value = value.get("$dec")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


async def _injections(
    session: AsyncSession, scope: Scope, lo: int, hi: int,
) -> list[dict[str, Any]]:
    rows = await _rows(session, """
        SELECT payload->'data' AS data FROM sim_venue_event
        WHERE exchange_account_id = :account_text AND deployment_environment = :realm
          AND event_type = 'fault_injected'
          AND (payload->'data'->>'mts')::bigint BETWEEN :lo AND :hi
        ORDER BY seq""", {**scope.params, "lo": lo, "hi": hi})
    return [dict(r.data) for r in rows]


def match_injected_attempts(
    attempts: Sequence[Attempt], injections: Sequence[Mapping[str, Any]],
) -> set[UUID]:
    """The attempts that an unused submit injection of an UNKNOWN fault belongs to.

    An injection belongs to an attempt when it asked for the same symbol, amount, rate and
    period AND happened inside the attempt's interval; each injection explains at most one
    attempt, the earliest attempt first. There is no time slack on purpose: the bot and the
    venue run in one process on one clock, the attempt is stamped before it sends and after it
    hears back, and the venue stamps the injection in between. A clock step then makes an
    injected attempt look organic (FAIL), the safe direction; slack would let a nearby organic
    UNKNOWN steal the match.
    """
    pool = sorted(
        (i for i in injections
         if i.get("target") == "submit" and i.get("fault_kind") in UNKNOWN_FAULT_KINDS),
        key=lambda i: int(i["mts"]))
    injected: set[UUID] = set()
    for attempt in sorted(attempts, key=lambda a: (a.started, a.completed)):
        for index, item in enumerate(pool):
            if (attempt.started <= int(item["mts"]) <= attempt.completed
                    and item.get("symbol") == attempt.symbol
                    and _decimal(item.get("amount")) == attempt.amount
                    and _decimal(item.get("rate")) == attempt.rate
                    and item.get("period") == attempt.period):
                injected.add(attempt.attempt_id)
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
    lo = window.since_ms + KILL_WINDOW_START_HOUR * HOUR_MS
    hi = window.since_ms + KILL_WINDOW_END_HOUR * HOUR_MS
    timed = [h for h in kills if lo <= h.created_at_ms <= hi]
    kill_criterion = Criterion(
        "d3.operator_kill",
        f">= {MIN_OPERATOR_KILLS} operator HALT with an acknowledged cancel-all at hour "
        f"{KILL_WINDOW_START_HOUR}-{KILL_WINDOW_END_HOUR}",
        _verdict(len(timed) >= MIN_OPERATOR_KILLS),
        {"operator_halts": len(halts), "with_acknowledged_cancel_all": len(kills),
         "in_kill_window": len(timed),
         "kill_hours_after_start": [round((h.created_at_ms - window.since_ms) / HOUR_MS, 2)
                                    for h in kills],
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
    """Accepted / queries, with the cycles an injected history failure fell into taken out.

    A cycle spans its query's start to its observation's end (to the next query's start when it
    never produced an observation: a crash or restart mid-cycle; to the window's end for the last). Those spanning a ``history``
    injection are counted apart; the gated ratio is over the organic cycles, and a query without
    an observation still counts as not accepted there.
    """
    rows = await _rows(session, """
        SELECT * FROM (
          SELECT q.started_at_ms AS started, o.accepted AS accepted,
                 o.confirmation_finished_at_ms AS finished,
                 lead(q.started_at_ms) OVER (ORDER BY q.query_revision) AS next_start
          FROM ledger_observation_query q
          LEFT JOIN ledger_observation o ON o.query_id = q.query_id AND o.accepted
          WHERE q.exchange_account_id = :account AND q.deployment_environment = :realm
        ) cycles WHERE started BETWEEN :t0 AND :t1 ORDER BY started""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    history = sorted(
        int(i["mts"]) for i in await _injections(session, scope, window.since_ms, window.until_ms)
        if i.get("target") == "history")
    organic = accepted = injected = injected_accepted = 0
    for row in rows:
        end = row.finished if row.finished is not None else (
            row.next_start if row.next_start is not None else window.until_ms)
        hit = bisect_right(history, end) > bisect_left(history, row.started)
        if hit:
            injected += 1
            injected_accepted += bool(row.accepted)
        else:
            organic += 1
            accepted += bool(row.accepted)
    ratio = Decimal(accepted) / Decimal(organic) if organic else Decimal(0)
    return Criterion(
        "d3.accepted_cycle_ratio", f">= {MIN_ACCEPTED_CYCLE_RATIO} over cycles without an injection",
        _verdict(organic > 0 and ratio >= MIN_ACCEPTED_CYCLE_RATIO),
        {"queries": len(rows), "organic_queries": organic, "organic_accepted": accepted,
         "ratio": f"{ratio:.4f}", "injected_history_cycles": injected,
         "injected_history_cycles_accepted_anyway": injected_accepted})


async def uncertainty(
    session: AsyncSession, scope: Scope, window: Window,
) -> list[Criterion]:
    """Organic versus injected UNKNOWN / quarantine, and whether the injected ones closed."""
    attempts = [Attempt(r.attempt_id, r.started, r.completed, r.symbol, _decimal(r.amount),
                        _decimal(r.rate), r.period) for r in await _rows(session, """
        SELECT a.attempt_id, a.started_at_ms AS started, t.completed_at_ms AS completed,
               a.symbol, a.normalized_payload->>'amount' AS amount,
               a.normalized_payload->>'rate' AS rate,
               (a.normalized_payload->>'period')::integer AS period
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
    # An attempt that began before the window may have been injected before it: look back to
    # its start (the injection lies inside the attempt's own interval).
    lo = min([window.since_ms, *(a.started for a in attempts)])
    hi = max([window.until_ms, *(a.completed for a in attempts)])
    injections = await _injections(session, scope, lo, hi)
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
    interest = await _scalar(session, """
        SELECT count(*) FROM sim_venue_event
        WHERE exchange_account_id = :account_text AND deployment_environment = :realm
          AND event_type = 'interest_paid'
          AND (payload->'data'->>'mts')::bigint BETWEEN :t0 AND :t1""",
        {**scope.params, "t0": window.since_ms, "t1": window.until_ms})
    fills, cancels = venue.get("offer_filled", 0), venue.get("offer_canceled", 0)
    return [
        Criterion("floor.acked_submits", f">= {FLOOR_ACKED_SUBMITS}",
                  _verdict(acked >= FLOOR_ACKED_SUBMITS), {"acked_submits": acked}),
        Criterion("floor.fills", f">= {FLOOR_FILLS}", _verdict(fills >= FLOOR_FILLS),
                  {"offer_filled": fills}),
        # reported only: cancels/reprices (prod ~0) and expiry settlement (CI oracle covers it)
        Criterion("info.cancels", "reported only", "PASS", {"offer_canceled": cancels}),
        Criterion("info.credits_closed_by_expiry", "reported only", "PASS",
                  {"credit_closed_expired": expired}),
        Criterion("floor.interest_payments", f">= {FLOOR_INTEREST_PAYMENTS} in the window",
                  _verdict(interest >= FLOOR_INTEREST_PAYMENTS), {"interest_paid": interest}),
    ]


async def simulator_failures(session: AsyncSession, scope: Scope, window: Window) -> list[Criterion]:
    """What the simulator got wrong, from its durable records (restart- and crash-proof)."""
    out = []
    for criterion_id, event_type, label in (
            ("extra.internal_failures", "internal_failure_recorded", "kind"),
            ("extra.unexpected_requests", "unexpected_request_recorded", "method")):
        rows = await _rows(session, f"""
            SELECT payload->'data'->>'{label}' AS label, count(*) AS n FROM sim_venue_event
            WHERE exchange_account_id = :account_text AND deployment_environment = :realm
              AND event_type = :event_type
              AND (payload->'data'->>'mts')::bigint BETWEEN :t0 AND :t1
            GROUP BY 1""", {**scope.params, "event_type": event_type,
                            "t0": window.since_ms, "t1": window.until_ms})
        by_label = {r.label: r.n for r in rows}
        out.append(Criterion(
            criterion_id, "== 0 in the venue log", _verdict(not by_label),
            {"total": sum(by_label.values()), f"by_{label}": by_label}))
    return out


def _read_generation(path: Path) -> dict[str, list[Any]]:
    """Family name -> samples of one saved ``/metrics`` scrape (families without samples kept)."""
    from prometheus_client.parser import text_string_to_metric_families

    families: dict[str, list[Any]] = {}
    for family in text_string_to_metric_families(path.read_text()):
        families.setdefault(family.name, []).extend(family.samples)
    return families


def feed_counters(metrics_files: Sequence[Path]) -> list[Criterion]:
    """The feed counters, which live only in a process, summed over every saved generation.

    A backfill that was cut short and continued (``trades_truncated``) is reported; one that
    could not be recovered (``trades_beyond_retention``: the downtime outlived the retention)
    lost trades for good and fails the criterion.
    """
    rule = "no unrecoverable trades gap (trades_beyond_retention == 0)"
    criterion_id = "extra.trades_backfill_gaps"
    if not metrics_files:
        return [Criterion(criterion_id, rule, "UNAVAILABLE", {"reason": "no metrics files"})]
    generations: list[dict[str, list[Any]]] = []
    try:
        for path in metrics_files:
            generations.append(_read_generation(path))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return [Criterion(criterion_id, rule, "UNAVAILABLE",
                          {"reason": f"metrics file unreadable: {type(exc).__name__}"})]
    missing = [str(p) for p, g in zip(metrics_files, generations, strict=True)
               if FAMILY_FEED_FAILURES not in g]
    if missing:
        return [Criterion(criterion_id, rule, "UNAVAILABLE",
                          {"reason": f"{FAMILY_FEED_FAILURES} missing", "files": missing})]

    def total(family: str, **labels: str) -> float:
        return float(sum(
            sample.value for generation in generations for sample in generation.get(family, [])
            if sample.name.endswith("_total")
            and all(sample.labels.get(k) == v for k, v in labels.items())))

    evidence: dict[str, Any] = {
        "generations": len(generations),
        "trades_beyond_retention": total(FAMILY_FEED_FAILURES, source="trades_beyond_retention"),
        "trades_truncated_then_continued": total(FAMILY_FEED_FAILURES, source="trades_truncated"),
        "book_fetch_failures": total(FAMILY_FEED_FAILURES, source="book"),
        "trades_fetch_failures": total(FAMILY_FEED_FAILURES, source="trades"),
    }
    if all(FAMILY_FEED_STREAM in g for g in generations):
        evidence["trades_ws_disconnects"] = total(FAMILY_FEED_STREAM, event="disconnected")
    lags = [s.value for g in generations for s in g.get(FAMILY_WATERMARK_LAG, [])]
    if lags:
        evidence["watermark_lag_seconds_at_last_scrape_max"] = max(lags)
    return [Criterion(criterion_id, rule, _verdict(evidence["trades_beyond_retention"] == 0),
                      evidence)]


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
    criteria += await simulator_failures(session, scope, window)
    criteria += feed_counters(metrics_files)
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
