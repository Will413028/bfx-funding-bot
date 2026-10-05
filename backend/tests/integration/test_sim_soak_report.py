"""``scripts/sim_soak_report.py`` on migrated PostgreSQL: one passing soak, then one broken
property at a time. Rows are seeded by the owner with the row triggers and foreign keys off
(``session_replication_role = replica``): the report reads what the soak wrote, so the seed
only has to satisfy the tables' CHECKs. Window and clock values are fixed numbers; nothing here
depends on wall-clock time.

Mutations (one at a time; revert after each): count injected UNKNOWN attempts as organic
(``test_without_the_injection_record_every_unknown_is_organic_and_nothing_proves_the_closure``);
drop the activity floor (``test_a_soak_that_never_submitted_fails_the_floor``); divide the cycle
ratio by observations instead of queries (``test_a_query_without_an_observation_is_not_accepted``);
drop the window filter (``test_rows_outside_the_window_do_not_count``); read a missing counter
as zero (``test_missing_counters_are_unavailable_never_zero``, ``test_unusable_metrics_*``); treat ``foreign_lending`` as
conserved (``test_unexplained_and_foreign_lending_each_fail``).
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.observability.metrics import DaemonMetrics
from bfx_funding_bot.modules.simulated_venue import events as ev
from scripts import sim_soak_report as report

from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture dependency

pytestmark = pytest.mark.integration

ACCOUNT = UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
REALM = "ci"  # the CLI only accepts ``shadow``; tests stamp ``ci`` like the other sim tests
HOUR = 3_600_000
D150, D0002 = Decimal("150"), Decimal("0.0002")
DAY = 86_400_000
T0 = 1_704_067_200_000  # 2024-01-01T00:00Z, a UTC midnight
WINDOW = report.Window(T0, T0 + 26 * HOUR)
CYCLE = 7 * 60_000  # a ledger query every 7 minutes: 200 of them fit the 24 h bar


def scrape(tmp_path: Path, name: str, *, disconnects: int = 0, truncated: int = 0,
           beyond_retention: int = 0) -> Path:
    """The saved ``/metrics`` text of one process generation, rendered by the real registry."""
    metrics = DaemonMetrics()
    observer = metrics.sim_venue_observer()
    for _ in range(disconnects):
        observer.feed_stream("disconnected")
    for _ in range(truncated):
        observer.feed_failure("trades_truncated")
    for _ in range(beyond_retention):
        observer.feed_failure("trades_beyond_retention")
    path = tmp_path / f"{name}.metrics"
    path.write_bytes(metrics.render())
    return path


@dataclass
class Soak:
    """What to seed: the defaults are a soak that passes everything."""

    revisions: Sequence[tuple[str, int]] = (
        ("rev-a", T0 + HOUR), ("rev-b", T0 + 12 * HOUR))
    kill_at: int | None = T0 + 8 * HOUR
    resume_at: int | None = T0 + 9 * HOUR
    cancel_all_acknowledged: bool = True
    queries: int = 200
    queries_without_observation: int = 2
    acked_submits: int = 20
    acked_from: int = 3 * HOUR  # offset of the first acked submit
    injected_unknown: int = 5
    record_injections: bool = True
    organic_unknown: int = 0
    organic_quarantine: int = 0
    injected_quarantine_open: bool = False
    leave_injected_open: int = 0
    fills: int = 12
    cancels: int = 12
    expired_credits: int = 1
    interest_days: Sequence[int] = (0, 1)
    conservation: Sequence[tuple[str, int]] = (("baseline", T0 + 2 * HOUR),
                                               ("conserved", T0 + 3 * HOUR))
    internal_failures: Sequence[int] = ()  # times of durable internal-failure records
    unexpected_requests: Sequence[int] = ()
    history_faults_in_cycles: Sequence[int] = ()  # cycle indexes that hold a history injection
    overlapping_organic: bool = False  # an organic UNKNOWN of other content beside injection 0
    seq: int = field(default=0, init=False)
    attempt_seq: int = field(default=0, init=False)
    injected_attempts: list[UUID] = field(default_factory=list, init=False)


async def _x(conn: AsyncConnection, sql: str, **params: Any) -> Any:
    return await conn.execute(text(sql), params)


async def _event(conn: AsyncConnection, soak: Soak, event: ev.VenueEvent) -> None:
    soak.seq += 1
    payload = ev.event_to_payload(event)
    await _x(conn, """
        INSERT INTO sim_venue_event (exchange_account_id, deployment_environment, seq,
                                     event_type, schema_version, payload)
        VALUES (:a, :r, :seq, :t, :v, CAST(:p AS jsonb))""",
             a=str(ACCOUNT), r=REALM, seq=soak.seq, t=payload["event_type"],
             v=payload["schema_version"], p=json.dumps(payload))


async def _attempt(
    conn: AsyncConnection, soak: Soak, *, kind: str, started: int, completed: int,
    amount: str = "150",
) -> UUID:
    soak.attempt_seq += 1
    attempt_id = uuid4()
    await _x(conn, """
        INSERT INTO submission_attempt_journal (attempt_id, execution_decision_id,
            exchange_account_id, deployment_environment, symbol, cell_id, attempt_seq,
            normalized_payload, payload_sha256, basis_id, policy_revision_id,
            authorization_evidence, started_at_ms)
        VALUES (:id, :decision, :a, :r, 'fUST', 'cell', :seq,
            CAST(:payload AS jsonb), 'sha', :basis, :policy, CAST('{}' AS jsonb), :started)""",
             id=attempt_id, decision=f"decision-{attempt_id}", a=ACCOUNT, r=REALM,
             seq=soak.attempt_seq, basis=uuid4(), policy=uuid4(), started=started,
             payload=json.dumps({"type": "LIMIT", "amount": amount, "rate": "0.0002",
                                 "period": 2, "flags": 0}))
    await _x(conn, """
        INSERT INTO transport_outcome_journal (attempt_id, kind, venue_offer_id, reason,
                                               completed_at_ms, evidence)
        VALUES (:id, :kind, :offer, :reason, :completed, CAST('{}' AS jsonb))""",
             id=attempt_id, kind=kind, offer=str(40_000_000 + soak.attempt_seq) if kind == "ack"
             else None, reason=None if kind == "ack" else "timeout", completed=completed)
    return attempt_id


async def _resolve(conn: AsyncConnection, *, attempt: UUID | None = None,
                   quarantine: UUID | None = None, action: str = "bound_to_venue") -> None:
    await _x(conn, """
        INSERT INTO execution_resolution_journal (id, attempt_id, quarantine_id,
            exchange_account_id, deployment_environment, symbol, action, venue_offer_id,
            observation_id, actor_kind, actor_id, resolved_at_ms, reason, evidence)
        VALUES (:id, :attempt, :quarantine, :a, :r, 'fUST', :action, :offer, :obs,
                'system', 'uncertainty_worker', 1, 'resolved', CAST('{}' AS jsonb))""",
             id=uuid4(), attempt=attempt, quarantine=quarantine, a=ACCOUNT, r=REALM,
             action=action, offer="1" if action == "bound_to_venue" else None, obs=uuid4())


async def _quarantine(conn: AsyncConnection, *, at: int, source: UUID | None) -> UUID:
    quarantine_id = uuid4()
    await _x(conn, """
        INSERT INTO quarantine_opening (quarantine_id, exchange_account_id,
            deployment_environment, symbol, intended_amount, opened_at_ms, opened_revision,
            evidence, source_attempt_id)
        VALUES (:id, :a, :r, 'fUST', 150, :at, 1, CAST('{}' AS jsonb), :source)""",
             id=quarantine_id, a=ACCOUNT, r=REALM, at=at, source=source)
    return quarantine_id


async def seed(conn: AsyncConnection, soak: Soak) -> None:
    await _x(conn, "SET LOCAL session_replication_role = replica")
    for index, (version, at) in enumerate(soak.revisions):
        await _x(conn, """
            INSERT INTO execution_decisions (decision_id, account_id, exchange_account_id,
                deployment_environment, reconcile_id, cell_id, symbol, signal_correlation_id,
                outcome, signal_rate, amount_usdt, duration_days, model_evidence, safety_result,
                execution_policy, service_version, config_hash, occurred_at_ms, recorded_at_ms)
            VALUES (:id, :account, :a, :r, 'r', 'cell', 'fUST', :sig, 'submitted', 0, 1, 2,
                CAST('{}' AS jsonb), CAST('{}' AS jsonb), 'policy', :version, 'hash', :at, :at)""",
                 id=f"d-{index}", account=str(ACCOUNT), a=ACCOUNT, r=REALM, sig=f"s-{index}",
                 version=version, at=at)
    # trading state: the bootstrap ACTIVE, then the kill and the harness resume
    states = [("ACTIVE", "operator", "bootstrap_simulation_db", T0 - HOUR)]
    if soak.kill_at is not None:
        states.append(("HALTED", "operator", "soak-kill", soak.kill_at))
    if soak.resume_at is not None:
        states.append(("ACTIVE", "operator", "soak-orchestrator", soak.resume_at))
    for state, cause, actor, at in states:
        row = (await _x(conn, """
            INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause,
                                       actor, reason, created_at_ms)
            VALUES (:a, :r, :state, :cause, :actor, 'soak', :at) RETURNING id""",
                        a=ACCOUNT, r=REALM, state=state, cause=cause, actor=actor, at=at)).one()
        if actor == "soak-kill":
            await _x(conn, """
                INSERT INTO funding_cancel_all_audit (exchange_account_id, deployment_environment,
                    trading_state_id, attempt_id, currency, phase, venue_status, actor,
                    occurred_at_ms)
                VALUES (:a, :r, :sid, :attempt, 'UST', :phase, :status, 'soak-kill', :at)""",
                     a=ACCOUNT, r=REALM, sid=row.id, attempt=uuid4(), at=at,
                     phase="acknowledged" if soak.cancel_all_acknowledged else "requested",
                     status="SUCCESS" if soak.cancel_all_acknowledged else None)
    # ledger cycles: a query every 7 minutes; the last ones never produced an observation
    for index in range(soak.queries):
        query_id = uuid4()
        started = T0 + 10 * 60_000 + index * CYCLE
        await _x(conn, """
            INSERT INTO ledger_observation_query (query_id, exchange_account_id,
                deployment_environment, query_revision, started_at_ms, start_revision)
            VALUES (:q, :a, :r, :rev, :at, 0)""",
                 q=query_id, a=ACCOUNT, r=REALM, rev=index + 1, at=started)
        if index < soak.queries - soak.queries_without_observation:
            await _x(conn, """
                INSERT INTO ledger_observation (id, query_id, exchange_account_id,
                    deployment_environment, schema_version, query_finished_at_ms,
                    confirmation_finished_at_ms, accept_revision, wallets_complete,
                    offers_complete, credits_complete, loans_complete, offer_history_complete,
                    credit_history_complete, trades_complete, first_digest, confirmation_digest,
                    accepted, evidence)
                VALUES (:id, :q, :a, :r, 1, :at, :at, 0, true, true, true, true, true, true, true,
                    'd', 'd', true, CAST('{}' AS jsonb))""",
                     id=uuid4(), q=query_id, a=ACCOUNT, r=REALM, at=started + 1_000)
    for verdict, at in soak.conservation:
        basis = uuid4()
        await _x(conn, """
            INSERT INTO accepted_capital_basis (id, exchange_account_id, deployment_environment,
                observation_id, accepted, accept_revision, attempt_seq_high_water, schema_version,
                digest, accepted_at_ms)
            VALUES (:id, :a, :r, :obs, true, 0, 0, 1, 'digest', :at)""",
                 id=basis, a=ACCOUNT, r=REALM, obs=uuid4(), at=at)
        lent, foreign, conflicts = {
            "baseline": (0, 0, 0), "conserved": (0, 0, 0), "foreign_lending": (0, 5, 0),
            "unexplained_lending": (7, 0, 0)}[verdict]
        await _x(conn, """
            INSERT INTO accepted_capital_basis_symbol (basis_id, symbol, available, offered,
                credits, unattributed_credits, foreign_offers, conservation, lent_unexplained,
                foreign_executed, fill_conflicts)
            VALUES (:id, 'fUST', 10, 0, 0, 0, 0, :verdict, :lent, :foreign, :conflicts)""",
                 id=basis, verdict=verdict, lent=lent, foreign=foreign, conflicts=conflicts)
    # submits: acked ones, then UNKNOWN ones (injected, or organic when asked)
    for index in range(soak.acked_submits):
        at = T0 + soak.acked_from + index * HOUR // 2
        await _attempt(conn, soak, kind="ack", started=at, completed=at + 500)
    for index in range(soak.injected_unknown + soak.organic_unknown):
        started = T0 + 4 * HOUR + index * HOUR
        attempt = await _attempt(conn, soak, kind="unknown", started=started,
                                 completed=started + 2_000)
        if index < soak.injected_unknown:
            soak.injected_attempts.append(attempt)
            if soak.record_injections:
                await _event(conn, soak, ev.FaultInjected(
                    "unknown_placed_lost", "submit", index + 1, 1, "fUST", D150, D0002, 2,
                    started + 1_000))
                if index == 0 and soak.overlapping_organic:
                    # another UNKNOWN, of another amount, that began first and ends last: it
                    # contains the injection in time but is not the request that was injected
                    stranger = await _attempt(conn, soak, kind="unknown", started=started - 500,
                                              completed=started + 3_000, amount="200")
                    await _resolve(conn, attempt=stranger, action="not_accepted")
            if index >= soak.leave_injected_open:
                await _resolve(conn, attempt=attempt)
        else:
            await _resolve(conn, attempt=attempt, action="not_accepted")
    await _event(conn, soak, ev.FaultInjected(
        "history_error", "history", 1, 1, None, None, None, None, T0 + 5 * HOUR))
    for number, cycle in enumerate(soak.history_faults_in_cycles, start=2):
        await _event(conn, soak, ev.FaultInjected(
            "history_error", "history", number, 1, None, None, None, None,
            T0 + 10 * 60_000 + cycle * CYCLE + 500))
    for at in soak.internal_failures:
        await _event(conn, soak, ev.InternalFailureRecorded("feed", "feed trades failed", at))
    for at in soak.unexpected_requests:
        await _event(conn, soak, ev.UnexpectedRequestRecorded("GET", "https://x.test/y", at))
    if soak.injected_quarantine_open:
        await _quarantine(conn, at=T0 + 5 * HOUR, source=soak.injected_attempts[0])
    for index in range(soak.organic_quarantine):
        await _quarantine(conn, at=T0 + 6 * HOUR + index, source=None)
    # the venue's own log: what happened on the simulated exchange
    for index in range(soak.fills):
        await _event(conn, soak, ev.OfferFilled(
            index + 1, index + 1, index + 1, 1, 0, 2, T0 + 5 * HOUR + index * HOUR))
    for index in range(soak.cancels):
        await _event(conn, soak, ev.OfferCanceled(index + 1, T0 + 5 * HOUR + index * HOUR))
    for index in range(soak.expired_credits):
        await _event(conn, soak, ev.CreditClosed("loan", index + 1, T0 + 20 * HOUR, "expired"))
    for day in soak.interest_days:
        await _event(conn, soak, ev.InterestPaid(
            day + 1, "UST", 1, 1, T0 + day * DAY + 90 * 60_000))


@pytest.fixture
async def engine(ledger_db):  # noqa: F811
    url = ledger_db.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    eng = create_async_engine(url)
    async with eng.begin() as conn:  # the owner's one-time stamp (the template is unstamped)
        await _x(conn, "SET LOCAL session_replication_role = replica")
        await _x(conn, "DELETE FROM database_realm")
        await _x(conn, "INSERT INTO database_realm (realm, stamped_at_ms, actor) "
                       "VALUES ('ci', 1, 'test')")
    yield eng
    await eng.dispose()


async def _report(engine, soak: Soak | None = None, *, window: report.Window = WINDOW,
                  metrics: Sequence[Path] | None = None, tmp_path: Path | None = None,
                  ) -> dict[str, report.Criterion]:
    async with engine.begin() as conn:
        await seed(conn, soak or Soak())
    if metrics is None:  # the default is two clean process generations
        assert tmp_path is not None
        metrics = [scrape(tmp_path, "gen1"), scrape(tmp_path, "gen2")]
    async with async_sessionmaker(engine)() as session:
        criteria = await report.build_report(
            session, report.Scope(ACCOUNT, REALM), window, metrics_files=metrics)
    by_id = {c.id: c for c in criteria}
    assert len(by_id) == len(criteria)  # every criterion is named once
    return by_id


def _statuses(by_id: dict[str, report.Criterion]) -> dict[str, str]:
    return {k: c.status for k, c in by_id.items()}


def _failing(by_id: dict[str, report.Criterion]) -> set[str]:
    return {k for k, c in by_id.items() if c.status != "PASS"}


async def test_a_soak_that_meets_the_bar_and_the_floor_passes_everything(engine, tmp_path) -> None:
    by_id = await _report(engine, tmp_path=tmp_path)
    assert _failing(by_id) == set(), _statuses(by_id)
    assert report.exit_code(list(by_id.values())) == 0
    assert by_id["d3.deploy_restarts"].evidence["deploy_restarts"] == 1
    assert by_id["d3.accepted_cycle_ratio"].evidence["ratio"] == "0.9900"
    assert by_id["d3.injected_unknown_auto_closed"].evidence["injected_unknown"] == 5
    assert by_id["d3.organic_quarantine_unknown"].evidence["organic_unknown"] == 0
    assert by_id["floor.interest_payments"].evidence["interest_paid"] == 2
    text_out = report.render(list(by_id.values()), event_count=3)
    assert text_out.splitlines()[-1] == "RESULT PASS" and "PASS" in text_out


async def test_a_soak_that_never_submitted_fails_the_floor(engine, tmp_path) -> None:
    """The safety criteria are satisfied by an idle bot; only the floor tells."""
    by_id = await _report(engine, Soak(
        acked_submits=0, injected_unknown=0, fills=0, cancels=0, expired_credits=0,
        interest_days=()), tmp_path=tmp_path)
    assert {"floor.acked_submits", "floor.fills", "floor.interest_payments"} <= _failing(by_id)
    assert by_id["d3.unexplained_lending"].status == "PASS"
    assert by_id["d3.organic_quarantine_unknown"].status == "PASS"
    assert report.exit_code(list(by_id.values())) == 1


@pytest.mark.parametrize(("change", "failing"), [
    ({"acked_submits": report.FLOOR_ACKED_SUBMITS - 1, "injected_unknown": 0,
      "record_injections": False}, "floor.acked_submits"),
    ({"fills": report.FLOOR_FILLS - 1}, "floor.fills"),
    ({"interest_days": ()}, "floor.interest_payments"),
])
async def test_each_floor_number_is_a_hard_threshold(engine, tmp_path, change, failing) -> None:
    by_id = await _report(engine, Soak(**change), tmp_path=tmp_path)
    assert failing in _failing(by_id), _statuses(by_id)
    assert {failing, "d3.injected_unknown_auto_closed", "amendment.trading_after_resume"} >= (
        _failing(by_id)), _statuses(by_id)


async def test_the_floor_is_met_exactly_at_its_numbers(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(
        acked_submits=report.FLOOR_ACKED_SUBMITS, acked_from=10 * HOUR, fills=report.FLOOR_FILLS,
        cancels=0, expired_credits=0, interest_days=(0,)), tmp_path=tmp_path)
    assert _failing(by_id) == set(), _statuses(by_id)


async def test_expiry_settlement_and_cancels_are_reported_but_never_fail(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(expired_credits=0, cancels=0), tmp_path=tmp_path)
    assert _failing(by_id) == set(), _statuses(by_id)
    assert by_id["info.credits_closed_by_expiry"].evidence == {"credit_closed_expired": 0}
    assert by_id["info.cancels"].evidence == {"offer_canceled": 0}


async def test_zero_injected_unknown_fails_the_bar(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(injected_unknown=0, record_injections=False),
                          tmp_path=tmp_path)
    assert _failing(by_id) == {"d3.injected_unknown_auto_closed"}, _statuses(by_id)


async def test_acked_submits_do_not_count_the_unknown_ones(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(acked_submits=3), tmp_path=tmp_path)
    assert by_id["floor.acked_submits"].evidence["acked_submits"] == 3  # not 3 + 5 injected


async def test_injected_unknown_is_separated_from_organic_by_the_recorded_injections(
        engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(organic_unknown=2), tmp_path=tmp_path)
    organic = by_id["d3.organic_quarantine_unknown"]
    assert organic.status == "FAIL"
    assert (organic.evidence["organic_unknown"], organic.evidence["injected_unknown"]) == (2, 5)
    assert by_id["d3.injected_unknown_auto_closed"].status == "PASS"  # injected ones still closed
    assert _failing(by_id) == {"d3.organic_quarantine_unknown"}


async def test_without_the_injection_record_every_unknown_is_organic_and_nothing_proves_the_closure(
        engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(record_injections=False), tmp_path=tmp_path)
    assert by_id["d3.organic_quarantine_unknown"].evidence["organic_unknown"] == 5
    assert by_id["d3.organic_quarantine_unknown"].status == "FAIL"
    assert by_id["d3.injected_unknown_auto_closed"].status == "FAIL"
    assert by_id["d3.injected_unknown_auto_closed"].evidence["injected_unknown"] == 0


async def test_an_injected_unknown_left_unresolved_fails_the_auto_close(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(leave_injected_open=1), tmp_path=tmp_path)
    closed = by_id["d3.injected_unknown_auto_closed"]
    assert closed.status == "FAIL" and closed.evidence["open_attempts"] == 1
    assert by_id["d3.organic_quarantine_unknown"].status == "PASS"  # still injected, not organic


async def test_an_injected_quarantine_counts_as_injected_and_must_close_too(
        engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(injected_quarantine_open=True), tmp_path=tmp_path)
    assert by_id["d3.organic_quarantine_unknown"].status == "PASS"
    assert by_id["d3.organic_quarantine_unknown"].evidence["injected_quarantine"] == 1
    assert by_id["d3.injected_unknown_auto_closed"].status == "FAIL"
    assert by_id["d3.injected_unknown_auto_closed"].evidence["open_quarantines"] == 1


async def test_an_organic_quarantine_fails(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(organic_quarantine=1), tmp_path=tmp_path)
    assert by_id["d3.organic_quarantine_unknown"].evidence["organic_quarantine"] == 1
    assert _failing(by_id) == {"d3.organic_quarantine_unknown"}


@pytest.mark.parametrize(("verdict", "failing"), [
    ("unexplained_lending", "d3.unexplained_lending"),
    ("foreign_lending", "extra.foreign_lending"),
])
async def test_unexplained_and_foreign_lending_each_fail(
        engine, tmp_path, verdict, failing) -> None:
    by_id = await _report(engine, Soak(conservation=(
        ("baseline", T0 + 2 * HOUR), (verdict, T0 + 3 * HOUR))), tmp_path=tmp_path)
    assert _failing(by_id) == {failing}, _statuses(by_id)


async def test_no_accepted_basis_at_all_is_not_a_clean_conservation(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(conservation=()), tmp_path=tmp_path)
    assert {"d3.unexplained_lending", "extra.foreign_lending"} <= _failing(by_id)


async def test_a_query_without_an_observation_is_not_accepted(engine, tmp_path) -> None:
    """3 of 200 queries never produced an observation: 197/200 = 98.5 % < 99 % (a ratio over
    the observations alone would read 100 %)."""
    by_id = await _report(engine, Soak(queries_without_observation=3), tmp_path=tmp_path)
    cycles = by_id["d3.accepted_cycle_ratio"]
    assert cycles.status == "FAIL"
    assert (cycles.evidence["organic_queries"], cycles.evidence["organic_accepted"]) == (200, 197)
    assert _failing(by_id) == {"d3.accepted_cycle_ratio"}


async def test_rows_outside_the_window_do_not_count(engine, tmp_path) -> None:
    """An unexplained basis a moment before the window and one after it are not this soak's."""
    by_id = await _report(engine, Soak(conservation=(
        ("baseline", T0 + 2 * HOUR), ("unexplained_lending", T0 - 60_000),
        ("unexplained_lending", WINDOW.until_ms + 1))), tmp_path=tmp_path)
    assert by_id["d3.unexplained_lending"].status == "PASS"
    assert by_id["d3.unexplained_lending"].evidence["basis_symbols"] == 1


async def test_the_window_bounds_are_inclusive(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(conservation=(
        ("baseline", T0 + 2 * HOUR), ("unexplained_lending", WINDOW.until_ms))),
        tmp_path=tmp_path)
    assert by_id["d3.unexplained_lending"].status == "FAIL"


async def test_the_window_must_be_long_enough(engine, tmp_path) -> None:
    short = report.Window(T0, T0 + report.MIN_WINDOW_HOURS * HOUR - 1)
    by_id = await _report(engine, window=short, tmp_path=tmp_path)
    assert by_id["d3.window_hours"].status == "FAIL"


def test_the_bar_is_24_hours_not_72() -> None:
    assert report.window_criterion(report.Window(T0, T0 + 71 * HOUR)).status == "PASS"
    assert report.window_criterion(report.Window(T0, T0 + 23 * HOUR)).status == "FAIL"


def test_a_window_of_exactly_the_minimum_passes() -> None:
    exact = report.Window(T0, T0 + report.MIN_WINDOW_HOURS * HOUR)
    assert report.window_criterion(exact).status == "PASS"


async def test_restarts_need_new_revisions_and_an_identified_one(engine, tmp_path) -> None:
    none = await _report(engine, Soak(revisions=(("rev-a", T0 + HOUR), ("rev-a", T0 + 12 * HOUR))),
                         tmp_path=tmp_path)  # restarted, but on the same revision
    assert none["d3.deploy_restarts"].status == "FAIL"
    assert none["d3.deploy_restarts"].evidence["deploy_restarts"] == 0


async def test_an_unidentified_revision_fails_the_restart_count(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(revisions=(
        ("rev-a", T0 + HOUR), ("unidentified", T0 + 12 * HOUR), ("rev-c", T0 + 20 * HOUR))),
        tmp_path=tmp_path)
    restarts = by_id["d3.deploy_restarts"]
    assert restarts.status == "FAIL" and restarts.evidence["unidentified"] == ["unidentified"]


@pytest.mark.parametrize(("change", "failing"), [
    ({"kill_at": None, "resume_at": None}, {"d3.operator_kill", "amendment.trading_after_resume"}),
    ({"cancel_all_acknowledged": False},  # a HALT whose cancel-all never completed is no kill
     {"d3.operator_kill", "amendment.trading_after_resume"}),
    ({"resume_at": None}, {"amendment.trading_after_resume"}),  # HALTED at the end
    ({"resume_at": T0 + 20 * HOUR}, {"amendment.trading_after_resume"}),  # only 6 h of trading
    ({"kill_at": T0 + 5 * HOUR, "resume_at": T0 + 6 * HOUR}, {"d3.operator_kill"}),  # too early
    ({"kill_at": T0 + 10 * HOUR + 1, "resume_at": T0 + 11 * HOUR}, {"d3.operator_kill"}),  # late
])
async def test_the_kill_and_the_trading_after_it_are_part_of_the_bar(
        engine, tmp_path, change, failing) -> None:
    by_id = await _report(engine, Soak(**change), tmp_path=tmp_path)
    assert _failing(by_id) == failing, _statuses(by_id)


async def test_trading_after_the_resume_needs_acked_submits_in_it(engine, tmp_path) -> None:
    # every acked submit happens before the resume at +9 h
    by_id = await _report(engine, Soak(acked_submits=4), tmp_path=tmp_path)
    after = by_id["amendment.trading_after_resume"]
    assert after.evidence["hours_after_resume"] == 17.0
    assert after.evidence["acked_submits_after_resume"] == 0 and after.status == "FAIL"


async def test_without_metrics_only_the_feed_criterion_is_unavailable(engine) -> None:
    """Internal failures and unrouted requests come from the database; a generation whose
    /metrics was not saved can only make the feed criterion UNAVAILABLE, never a pass."""
    by_id = await _report(engine, metrics=[])
    assert _failing(by_id) == {"extra.trades_backfill_gaps"}
    assert by_id["extra.trades_backfill_gaps"].status == "UNAVAILABLE"
    assert by_id["extra.internal_failures"].status == "PASS"
    assert by_id["extra.unexpected_requests"].status == "PASS"
    assert report.exit_code(list(by_id.values())) == 3


def _file(tmp_path: Path, name: str, content: str | bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


@pytest.mark.parametrize("content", [
    "# nothing of the simulator in this scrape\nbfx_up 1\n",  # the family is absent
    b"\xff\xfe not text",
    "bfx_sim_venue_feed_failures_total{source=\"book\" 1\n",  # malformed
])
async def test_unusable_metrics_are_unavailable_not_zero(engine, tmp_path, content) -> None:
    good = scrape(tmp_path, "good")
    by_id = await _report(engine, metrics=[good, _file(tmp_path, "bad.metrics", content)])
    assert by_id["extra.trades_backfill_gaps"].status == "UNAVAILABLE"
    assert _failing(by_id) == {"extra.trades_backfill_gaps"}
    assert report.exit_code(list(by_id.values())) == 3


async def test_a_scrape_with_no_sample_is_zero_because_the_family_is_present(
        engine, tmp_path) -> None:
    clean = scrape(tmp_path, "clean")
    assert b"# TYPE bfx_sim_venue_feed_failures_total counter" in clean.read_bytes()
    assert b"bfx_sim_venue_feed_failures_total{" not in clean.read_bytes()
    by_id = await _report(engine, metrics=[clean])
    assert by_id["extra.trades_backfill_gaps"].status == "PASS"
    assert by_id["extra.trades_backfill_gaps"].evidence["trades_beyond_retention"] == 0.0


async def test_feed_counters_are_summed_over_generations_and_an_unrecoverable_gap_fails(
        engine, tmp_path) -> None:
    by_id = await _report(engine, metrics=[
        scrape(tmp_path, "gen1", truncated=2, disconnects=1),
        scrape(tmp_path, "gen2", beyond_retention=1, disconnects=3)])
    gaps = by_id["extra.trades_backfill_gaps"]
    assert gaps.status == "FAIL"
    assert gaps.evidence["trades_beyond_retention"] == 1.0
    assert gaps.evidence["trades_truncated_then_continued"] == 2.0
    assert gaps.evidence["trades_ws_disconnects"] == 4.0
    assert _failing(by_id) == {"extra.trades_backfill_gaps"}


async def test_a_truncated_backfill_that_continued_is_reported_but_not_a_failure(
        engine, tmp_path) -> None:
    by_id = await _report(engine, metrics=[scrape(tmp_path, "gen1", truncated=3)])
    assert by_id["extra.trades_backfill_gaps"].status == "PASS"
    assert by_id["extra.trades_backfill_gaps"].evidence["trades_truncated_then_continued"] == 3.0


async def test_internal_failures_come_from_the_database_whatever_the_metrics_say(
        engine, tmp_path) -> None:
    """Durable records: a failure of an earlier process life (its counter is gone) still counts,
    and one outside the window does not."""
    inside = T0 + 20 * HOUR
    by_id = await _report(engine, Soak(
        internal_failures=[inside, inside + 1, T0 - HOUR, WINDOW.until_ms + 1],
        unexpected_requests=[inside]), tmp_path=tmp_path)
    assert by_id["extra.internal_failures"].status == "FAIL"
    assert by_id["extra.internal_failures"].evidence == {"total": 2, "by_kind": {"feed": 2}}
    assert by_id["extra.unexpected_requests"].status == "FAIL"
    assert by_id["extra.unexpected_requests"].evidence["total"] == 1
    assert _failing(by_id) == {"extra.internal_failures", "extra.unexpected_requests"}


async def test_cycles_hit_by_an_injected_history_failure_leave_the_gated_ratio(
        engine, tmp_path) -> None:
    """3 of 200 queries never became an observation, and an injected history failure sits in
    each: the gated ratio is over the other 197 (all accepted), the 3 are reported apart.
    Mutation: counting them in the denominator."""
    by_id = await _report(engine, Soak(
        queries_without_observation=3, history_faults_in_cycles=(197, 198, 199)),
        tmp_path=tmp_path)
    cycles = by_id["d3.accepted_cycle_ratio"]
    assert cycles.status == "PASS" and cycles.evidence["ratio"] == "1.0000"
    assert cycles.evidence["injected_history_cycles"] == 3
    assert cycles.evidence["organic_queries"] == 197 and cycles.evidence["queries"] == 200


async def test_an_organic_failed_cycle_still_counts_beside_the_injected_ones(
        engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(
        queries_without_observation=4, history_faults_in_cycles=(198, 199)), tmp_path=tmp_path)
    cycles = by_id["d3.accepted_cycle_ratio"]
    assert cycles.evidence["injected_history_cycles"] == 2
    assert cycles.evidence["organic_queries"] == 198 and cycles.evidence["organic_accepted"] == 196
    assert cycles.status == "FAIL"  # 196 / 198 = 98.99 %


async def test_a_history_injection_between_cycles_excludes_nothing(engine, tmp_path) -> None:
    by_id = await _report(engine, Soak(queries_without_observation=3), tmp_path=tmp_path)
    assert by_id["d3.accepted_cycle_ratio"].evidence["injected_history_cycles"] == 0
    assert by_id["d3.accepted_cycle_ratio"].status == "FAIL"


async def test_an_organic_unknown_inside_an_injected_interval_stays_organic(
        engine, tmp_path) -> None:
    """The stranger's interval holds injection 0 but it asked for another amount."""
    by_id = await _report(engine, Soak(overlapping_organic=True), tmp_path=tmp_path)
    organic = by_id["d3.organic_quarantine_unknown"]
    assert organic.evidence["organic_unknown"] == 1 and organic.evidence["injected_unknown"] == 5
    assert _failing(by_id) == {"d3.organic_quarantine_unknown"}


async def test_the_report_refuses_a_database_that_is_not_the_simulation_realm(
        engine, tmp_path) -> None:
    url = engine.url.render_as_string(hide_password=False)
    with pytest.raises(report.ReportRefused, match="database_realm_not_simulation stamp=ci"):
        await report.run(database_url=url, account=ACCOUNT, window=WINDOW)


async def test_run_reads_the_database_end_to_end(engine, tmp_path) -> None:
    async with engine.begin() as conn:
        await seed(conn, Soak())
    url = engine.url.render_as_string(hide_password=False)
    criteria, events = await report.run(
        database_url=url, account=ACCOUNT, window=WINDOW, realm="ci",
        metrics_files=[scrape(tmp_path, "gen1")])
    assert report.exit_code(criteria) == 0 and events > 0


# -- matching injected faults to attempts (no database) ------------------------------------


def _inj(mts: int, kind: str = "unknown_placed_lost", target: str = "submit", *,
         amount: str = "150") -> dict[str, Any]:
    return {"mts": mts, "fault_kind": kind, "target": target, "symbol": "fUST",
            "amount": {"$dec": amount}, "rate": {"$dec": "0.0002"}, "period": 2}


def _att(started: int, completed: int, *, amount: str = "150") -> report.Attempt:
    return report.Attempt(uuid4(), started, completed, "fUST", Decimal(amount),
                          Decimal("0.0002"), 2)


def test_each_injection_explains_at_most_one_attempt() -> None:
    a, b = _att(1_000, 1_500), _att(1_200, 1_700)
    assert len(report.match_injected_attempts([a, b], [_inj(1_300)])) == 1
    both = report.match_injected_attempts([a, b], [_inj(1_300), _inj(1_400)])
    assert both == {a.attempt_id, b.attempt_id}


def test_only_unknown_submit_injections_explain_an_unknown_attempt() -> None:
    attempt = _att(1_000, 1_500)
    assert report.match_injected_attempts([attempt], [_inj(1_200, "history_error", "history")]) == set()
    assert report.match_injected_attempts([attempt], [_inj(1_200, "rejected")]) == set()
    assert report.match_injected_attempts([attempt], [_inj(1_200, "unknown_5xx_error")])


def test_an_injection_outside_the_attempts_interval_explains_nothing_there_is_no_slack() -> None:
    attempt = _att(1_000, 1_500)
    assert report.match_injected_attempts([attempt], [_inj(999)]) == set()
    assert report.match_injected_attempts([attempt], [_inj(1_501)]) == set()
    assert report.match_injected_attempts([attempt], [_inj(1_000)])  # the bounds are inclusive
    assert report.match_injected_attempts([attempt], [_inj(1_500)])


def test_a_nearby_organic_unknown_cannot_steal_the_match_by_time() -> None:
    """The stranger began first and its interval holds the injection too; matching by time
    alone gave it the injection (earliest attempt first) and left the injected attempt
    organic. By content the injection belongs to the attempt that asked for it."""
    stranger = _att(900, 2_500, amount="200")
    injected = _att(1_000, 2_000, amount="150")
    got = report.match_injected_attempts([stranger, injected], [_inj(1_500, amount="150")])
    assert got == {injected.attempt_id}


@pytest.mark.parametrize("change", [
    {"symbol": "fUSD"}, {"rate": {"$dec": "0.0003"}}, {"period": 30}, {"amount": None},
])
def test_every_field_of_the_content_must_agree(change: dict[str, Any]) -> None:
    attempt = _att(1_000, 1_500)
    assert report.match_injected_attempts([attempt], [{**_inj(1_200), **change}]) == set()


def test_decimals_compare_by_value_not_by_spelling() -> None:
    attempt = _att(1_000, 1_500, amount="150.0")
    assert report.match_injected_attempts([attempt], [_inj(1_200, amount="150")])
