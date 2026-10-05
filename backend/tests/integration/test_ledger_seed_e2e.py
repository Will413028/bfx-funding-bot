"""Legacy -> ledger closure seed (S1-4d), end to end through a legacy then a ledger bot process.

A legacy bot process builds every closure case on the HTTP-level fake venue; the process
stops; the real seed command (``apps.ledger_seed.run``) seeds as the table owner; the owner
flips the epoch; the venue moves during the halt; a ledger bot process boots and takes the
first real observation, whose basis is judged against the seed basis.

Closure cases (one scope, fUST): a live offer partially filled (its fill is a loan); a
claim-only live offer (legacy intent without an attempt); a fully filled offer whose credit
legacy attributed by synced funding trades; a multi-cell ``recent_fill`` group; an offer that
rests at seed time and fills during the halt (F1 evidence); an open UNKNOWN; the legacy tail
after the final fence (an acknowledged offer no snapshot reflected, a rejected submit); pending
requests of each table; an auto HALTED trading state. During the halt the loan becomes two
credits with the same (period, opening).

Declared divergence (F7, ruling 2026-10-05): a seeded ``recent_fill`` group stays multi-cell
under the ledger (it is carried; the venue trades that created it predate the window, so the
fake venue serves no trade older than the seed's query).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.execution.amount_fingerprint import fingerprints_in_use
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyRequestRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed, ReservationIntent
from bfx_funding_bot.modules.execution.safety.tables import (
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import append_transition
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
    UncertaintyResolutionRequestRow,
)
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import (
    QuarantineOpeningRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
    VenueOfferMirrorRow,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_ledger_managed_offers,
    build_ledger_uncertainties,
)
from bfx_funding_bot.modules.live_validation.tables import FundingTradeRow
from bfx_funding_bot.modules.trading import fingerprint_of

from .bot_e2e import (
    CELL,
    SCOPE,
    T0,
    AllowGuard,
    BotEnv,
    bot_env,  # noqa: F401 - fixture
    ledger_db,  # noqa: F401 - fixture dependency
    lost_response,
)
from .seed_e2e import (
    CELL_B,
    acked,
    basis_view,
    boot_as_epoch,
    credit_row,
    decision,
    flip_epoch,
    latest_bases,
    offer_row,
    rejected,
    run_seed,
    seed_command,
    submit,
    trade_row,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ZERO = Decimal(0)
# Every submitted amount carries a D3a fingerprint (last four of eight decimals).
LIVE = Decimal("300.00000101")      # 7001 cell a: 100 fills before the seed (a loan)
RECENT_A = Decimal("300.00000102")  # 7002 cell a: 200 fills
RECENT_B = Decimal("300.00000103")  # 7003 cell b: 200 fills
FILLED = Decimal("200.00000104")    # 7004 cell a: fills completely, synced trade
HALT = Decimal("250.00000105")      # 7005 cell b: rests at seed time, fills during the halt
CLAIM = Decimal("120.00000106")     # 7006 cell a: claim only, no attempt
UNKNOWN = Decimal("180.00000107")    # tail: submit answered 5xx, stays open
TAIL_ACK = Decimal("160.00000108")   # 7007 cell b: acknowledged after the final snapshot
TAIL_REJECTED = Decimal("170.00000109")
LOAN = Decimal("100")
RECENT_CREDIT = Decimal("150")
LOAN_OPENING, RECENT_OPENING, FILLED_AT = T0 + 40_000, T0 + 50_000, T0 + 55_000
SEED_AT, HALT_FILL_AT, RUNNER_AT = T0 + 100_000, T0 + 110_000, T0 + 150_000


@pytest.fixture
def authority() -> str:
    return "legacy"


async def stop(daemon: Any) -> None:
    """The legacy process exits: its writer lock is released."""
    await daemon.writer_lock.release()
    daemon.writer_lock = None


@dataclass
class Legacy:
    """What the legacy phase left, for the assertions."""

    claim_decision: str
    unknown_attempt: UUID | None
    tail_ack_attempt: UUID
    tail_rejected_attempt: UUID
    uncertainty_request: UUID | None
    policy_request: UUID
    control_request: UUID
    trading_state_id: int


async def attempt_of(env: BotEnv, amount: Decimal) -> UUID:
    async with env.factory() as session:
        rows = [row for row in await session.scalars(select(SubmissionAttemptRow))
                if Decimal(str(row.normalized_payload["amount"])) == amount]
    (row,) = rows
    return row.attempt_id


async def claim_only_offer(env: BotEnv, *, at: int) -> str:
    """A legacy intent without an attempt and its claim: an offer legacy owns by claim only."""
    decision_id, correlation = await decision(env, CLAIM, CELL, at)
    repository = CapitalRepository(account_id=SCOPE.exchange_account_id, environment="ci",
                                   max_snapshot_age_ms=300_000)
    cid = 900_001
    account = str(SCOPE.exchange_account_id)
    async with env.factory.begin() as session:
        await repository.writer.append(session, ReservationIntent(
            symbol="fUST", cid=cid, signal_correlation_id=correlation, account_id=account,
            is_simulated=False, execution_decision_id=decision_id,
            reservation_ref=ReservationRef(decision_id, cid, correlation), amount=CLAIM,
            occurred_at_ms=at))
        await repository.writer.append(session, ReservationClaimed(
            symbol="fUST", cid=cid, venue_offer_id="7006", signal_correlation_id=correlation,
            account_id=account, is_simulated=False, amount=CLAIM, occurred_at_ms=at + 500,
            reservation_ref=ReservationRef(decision_id, cid, correlation, "7006")))
    return decision_id


async def run_legacy(env: BotEnv, *, unknown: bool = True) -> Legacy:
    """Every closure case, through the legacy process, ending with the process stopped.

    ``unknown=False`` leaves out the open UNKNOWN (and the uncertainty request citing it), so
    fUST is not blocked and capital values can be compared (S1-4e)."""
    venue = env.venue
    venue.wallets = [["funding", "UST", "5000", 0, "5000"]]
    daemon = await env.build()
    await env.boot(daemon, T0)

    env.clock.now = T0 + 10_000
    for amount, offer_id, cell in ((LIVE, 7001, CELL), (RECENT_A, 7002, CELL),
                                   (RECENT_B, 7003, CELL_B), (FILLED, 7004, CELL),
                                   (HALT, 7005, CELL_B)):
        await submit(env, daemon, amount, acked(offer_id), cell=cell)
    placed = T0 + 10_000
    venue.offers = [offer_row(i, a, a, placed, placed) for i, a in (
        (7001, LIVE), (7002, RECENT_A), (7003, RECENT_B), (7004, FILLED), (7005, HALT))]
    await env.tick(daemon, T0 + 20_000)
    assert daemon.periodic_reconcile._non_accepted == 0

    # Fills before the seed: 7001 partly (a loan), 7002/7003 partly (a credit no trade has
    # been synced for: recent_fill on both cells), 7004 completely (its trade is synced).
    venue.offers = [
        offer_row(7001, LIVE, LIVE - LOAN, placed, LOAN_OPENING, "PARTIALLY FILLED"),
        offer_row(7002, RECENT_A, RECENT_A - 200, placed, RECENT_OPENING, "PARTIALLY FILLED"),
        offer_row(7003, RECENT_B, RECENT_B - 200, placed, RECENT_OPENING, "PARTIALLY FILLED"),
        offer_row(7005, HALT, HALT, placed, placed),
    ]
    venue.history_offers = [offer_row(7004, FILLED, ZERO, placed, FILLED_AT,
                                      "EXECUTED @ 0.0001(200.00000104)")]
    venue.loans = [credit_row(5001, LOAN, LOAN_OPENING)]
    venue.credits = [credit_row(8002, RECENT_CREDIT, RECENT_OPENING),
                     credit_row(8003, FILLED, FILLED_AT)]
    async with env.factory.begin() as session:
        session.add(FundingTradeRow(
            exchange_account_id=SCOPE.exchange_account_id, trade_id=9004,
            deployment_environment="ci", symbol="fUST", mts_create=FILLED_AT, offer_id=7004,
            amount=FILLED, rate=Decimal("0.0001"), period_days=2, maker=True))
    await env.tick(daemon, T0 + 60_000)
    assert daemon.periodic_reconcile._non_accepted == 0

    claim_decision = await claim_only_offer(env, at=T0 + 65_000)
    venue.offers.append(offer_row(7006, CLAIM, CLAIM, T0 + 65_500, T0 + 65_500))
    await env.tick(daemon, T0 + 80_000)  # the final legacy snapshot
    assert daemon.periodic_reconcile._non_accepted == 0
    async with env.factory() as session:
        final = await session.scalar(select(CapitalSnapshotRow).order_by(
            CapitalSnapshotRow.event_seq.desc()).limit(1))
    assert final is not None
    groups = final.classification["credit_cells"]
    assert {(g["basis"], tuple(g["cells"])) for g in groups.values()} == {
        ("recent_fill", (CELL, CELL_B)), ("funding_trade", (CELL,)),
    }, groups

    # The legacy tail after the final fence: an acknowledged offer no snapshot reflected, a
    # rejected submit, then a submit answered 5xx (an open UNKNOWN; it blocks fUST, so last).
    env.clock.now = T0 + 90_000
    await submit(env, daemon, TAIL_ACK, acked(7007), cell=CELL_B)
    venue.offers.append(offer_row(7007, TAIL_ACK, TAIL_ACK, T0 + 90_000, T0 + 90_000))
    env.clock.now = T0 + 92_000
    await submit(env, daemon, TAIL_REJECTED, rejected())
    if unknown:
        env.clock.now = T0 + 95_000
        await submit(env, daemon, UNKNOWN, lost_response())
    await stop(daemon)

    unknown_attempt = await attempt_of(env, UNKNOWN) if unknown else None
    requests = (uuid4(), uuid4(), uuid4())
    async with env.factory.begin() as session:
        if unknown:
            (uncertainty,) = await session.scalars(
                select(ExecutionUncertaintyRow.uncertainty_id)
                .where(ExecutionUncertaintyRow.state == "open"))
            session.add(UncertaintyResolutionRequestRow(
                request_id=requests[0], exchange_account_id=SCOPE.exchange_account_id,
                deployment_environment="ci", uncertainty_id=uncertainty,
                action="mark_not_accepted", reconcile_event_seq=final.event_seq,
                requested_by="operator-e2e", created_at_ms=T0 + 96_000))
        session.add(CapitalPolicyRequestRow(
            request_id=requests[1], exchange_account_id=SCOPE.exchange_account_id,
            deployment_environment="ci", symbol="fUSD", action="enable", reason="carry",
            requested_by="operator-e2e", created_at_ms=T0 + 96_000))
        session.add(TradingControlRequestRow(
            request_id=requests[2], exchange_account_id=SCOPE.exchange_account_id,
            deployment_environment="ci", action="kill", reason="carry",
            requested_by="operator-e2e", created_at_ms=T0 + 96_000))
    async with env.factory.begin() as session:
        await acquire_transaction_lock(session, account_id=str(SCOPE.exchange_account_id),
                                       deployment_environment="ci")
        await append_transition(session, account_id=SCOPE.exchange_account_id,
                                environment="ci", state="HALTED", cause="auto",
                                actor="system:protection", reason="seed-e2e",
                                now_ms=T0 + 97_000)
    async with env.factory() as session:
        trading_state = await session.scalar(select(func.max(TradingStateRow.id)))
    assert trading_state is not None
    return Legacy(claim_decision, unknown_attempt, await attempt_of(env, TAIL_ACK),
                  await attempt_of(env, TAIL_REJECTED), requests[0] if unknown else None,
                  requests[1], requests[2], int(trading_state))


def halt_moves(env: BotEnv) -> None:
    """During the halt: 7005 fills completely; the loan becomes two credits (same opening)."""
    venue = env.venue
    placed = T0 + 10_000
    venue.offers = [row for row in venue.offers if row[0] != 7005]
    venue.history_offers = [*venue.history_offers,
                            offer_row(7005, HALT, ZERO, placed, HALT_FILL_AT,
                                      "EXECUTED @ 0.0001(250.00000105)")]
    venue.loans = []
    venue.credits = [*venue.credits, credit_row(8101, Decimal("60"), LOAN_OPENING, T0 + 105_000),
                     credit_row(8102, Decimal("40"), LOAN_OPENING, T0 + 105_000),
                     credit_row(8005, HALT, HALT_FILL_AT)]
    # Only the halt's trade: the trades that made the seeded groups predate the window.
    venue.trades = [trade_row(9005, 7005, HALT, HALT_FILL_AT)]


async def legacy_fingerprints(env: BotEnv) -> frozenset[int]:
    async with env.factory() as session:
        return await fingerprints_in_use(session, account_id=SCOPE.exchange_account_id,
                                         environment="ci", symbol="fUST")


async def ledger_fingerprints(env: BotEnv) -> frozenset[int]:
    async with env.factory() as session:
        amounts = await build_ledger_managed_offers().fingerprints_in_use(session, SCOPE, "fUST")
    return frozenset(f for f in (fingerprint_of(a) for a in amounts) if f)


async def test_the_first_real_basis_judges_the_seed_basis_and_the_halt_fill(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    legacy = await run_legacy(env)
    legacy_prints = await legacy_fingerprints(env)

    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0, lines
    seed_line, verification, summary = lines
    assert verification["mismatches"] == [] and summary["committed"] is True
    assert seed_line["watermarks"]["trading_state_max_id"] == legacy.trading_state_id
    assert seed_line["failed_requests"] == 1
    assert seed_line["failed_uncertainty_requests"] == [str(legacy.uncertainty_request)]
    assert seed_line["carried_requests"] == {
        "capital_policy_requests": [str(legacy.policy_request)],
        "trading_control_requests": [str(legacy.control_request)],
    }

    # ---- at the capture point: the seed is the legacy closure
    (seeded,) = await latest_bases(env)
    seed = await basis_view(env, seeded.id)
    assert seeded.attempt_seq_high_water == max(
        row["attempt_seq"] for row in await _attempts(env))
    assert seed["groups"] == {
        ("credit", "8002"): (2, RECENT_OPENING, "recent_fill", frozenset({CELL, CELL_B})),
        ("credit", "8003"): (2, FILLED_AT, "trade", frozenset({CELL})),
        ("loan", "5001"): (2, LOAN_OPENING, "recent_fill", frozenset({CELL, CELL_B})),
    }
    assert seed["attempts"][legacy.unknown_attempt] == "unresolved"
    assert seed["attempts"][legacy.tail_ack_attempt] == "unresolved"  # the tail: next basis
    assert seed["attempts"][legacy.tail_rejected_attempt] == "unresolved"
    claim = next(row for row in await _attempts(env)
                 if row["execution_decision_id"] == legacy.claim_decision)
    assert claim["policy_revision_id"] is None
    assert claim["seed_provenance"]["legacy"] == "offer_claim"
    assert claim["normalized_payload"] == {
        "symbol": "fUST", "amount": str(CLAIM), "rate": "0.0001", "period": 2, "type": "LIMIT",
        "flags": {"raw": 0}}  # type and flags as the legacy snapshot observed them
    assert seed["attempts"][claim["attempt_id"]] == "reflected"
    assert all(row["seed_provenance"] and row["policy_revision_id"] is None
               for row in await _attempts(env))
    unknown = next(row for row in await _attempts(env)
                   if row["attempt_id"] == legacy.unknown_attempt)
    assert "uncertainty_id" in unknown["seed_provenance"]  # F8
    # Fingerprints (§C 6), each strict difference classified. Both hold the live managed
    # offers (claim-only included), the acknowledged tail offer and the open UNKNOWN. Legacy
    # alone still holds 7004's: its claim stays ``claimed`` after the offer executed, because
    # only the WS lifecycle path (off in this composition, ``skip_ws``) releases a filled
    # claim; the ledger frees it with the offer gone from the book. Nothing is ledger-only.
    ledger_prints = await ledger_fingerprints(env)
    assert legacy_prints - ledger_prints == {fingerprint_of(FILLED)}
    assert ledger_prints - legacy_prints == set()
    assert {fingerprint_of(a) for a in (LIVE, RECENT_A, RECENT_B, HALT, CLAIM, TAIL_ACK,
                                        UNKNOWN)} == ledger_prints
    # Requests (F9 + ruling 2026-10-02): the uncertainty request failed, the others carry.
    async with env.factory() as session:
        failed = await session.get(UncertaintyResolutionRequestRow, legacy.uncertainty_request)
        policy_request = await session.get(CapitalPolicyRequestRow, legacy.policy_request)
        control = await session.get(TradingControlRequestRow, legacy.control_request)
        latest_state = await session.scalar(select(func.max(TradingStateRow.id)))
        halted = await session.get(TradingStateRow, legacy.trading_state_id)
    assert failed is not None and (failed.state, failed.outcome_reason, failed.processed_at_ms) == (
        "failed", "superseded_by_authority_switch", SEED_AT)
    assert policy_request is not None and policy_request.state == "requested"
    assert control is not None and control.state == "requested"
    assert latest_state == legacy.trading_state_id  # the seed never writes trading_state
    assert halted is not None and (halted.state, halted.cause) == ("HALTED", "auto")

    await assert_capture_anti_joins(env)

    # ---- switch, the venue moves during the halt, the ledger process boots
    await flip_epoch(env, at=SEED_AT + 1_000)
    boot_as_epoch(env, monkeypatch)
    halt_moves(env)
    ledger = await env.build()
    await env.boot(ledger, RUNNER_AT)
    assert ledger.periodic_reconcile._non_accepted == 0
    runner, previous = await latest_bases(env)
    assert previous.id == seeded.id
    first = await basis_view(env, runner.id)
    fust = first["symbols"]["fUST"]
    # F1: the fill during the halt is explained by the seed basis's resting offer.
    assert (fust.conservation, fust.lent_unexplained, fust.fill_conflicts, fust.block) == (
        "conserved", ZERO, 0, None), (fust.conservation, fust.lent_unexplained, fust.block)
    assert runner.scope_block is None
    # Carried groups keep their cells (the loan's split credits carry its group); F7: the
    # seeded recent_fill group stays multi-cell.
    assert first["groups"] == {
        ("credit", "8002"): (2, RECENT_OPENING, "carry", frozenset({CELL, CELL_B})),
        ("credit", "8003"): (2, FILLED_AT, "carry", frozenset({CELL})),
        ("credit", "8101"): (2, LOAN_OPENING, "carry", frozenset({CELL, CELL_B})),
        ("credit", "8102"): (2, LOAN_OPENING, "carry", frozenset({CELL, CELL_B})),
        ("credit", "8005"): (2, HALT_FILL_AT, "trade", frozenset({CELL_B})),
    }
    # The tail is decided by the first real basis; the UNKNOWN stays open.
    assert first["attempts"] == {
        legacy.unknown_attempt: "unresolved",
        legacy.tail_ack_attempt: "reflected",
        legacy.tail_rejected_attempt: "settled",
    }
    async with env.factory() as session:
        r6 = await session.scalar(select(func.count()).select_from(QuarantineOpeningRow).where(
            QuarantineOpeningRow.source_attempt_id.is_not(None)))
        latest_state = await session.scalar(select(func.max(TradingStateRow.id)))
    assert r6 == 0
    assert latest_state == legacy.trading_state_id  # still auto HALTED after the first cycle
    assert await env.has_open(ledger)


async def assert_capture_anti_joins(env: BotEnv) -> None:
    """Pre-flight §C anti-joins 1, 2, 3 and 5 at the capture point, both directions."""
    async with env.factory() as session:
        final = await session.scalar(select(CapitalSnapshotRow).order_by(
            CapitalSnapshotRow.event_seq.desc()).limit(1))
        assert final is not None
        legacy_attempts = {row.attempt_id: row for row in await session.scalars(
            select(SubmissionAttemptRow))}
        claims = {(row.cid, row.execution_decision_id) for row in await session.scalars(
            select(OfferClaimRow))}
        open_legacy = {(row.symbol, row.attempt_id) for row in await session.scalars(
            select(ExecutionUncertaintyRow).where(ExecutionUncertaintyRow.state == "open"))}
        mirrors = {row.venue_offer_id: row for row in await session.scalars(
            select(VenueOfferMirrorRow))}
        open_ledger = await build_ledger_uncertainties().open_uncertainties(session, SCOPE)
    seeded = await _attempts(env)
    (basis,) = await latest_bases(env)
    classes = (await basis_view(env, basis.id))["attempts"]
    # 1. legacy live managed offers <-> seeded ack attempts <-> live mirror rows.
    ours = set(final.classification["offers"])
    acked_live = {row["venue_offer_id"] for row in seeded
                  if row["outcome"] == "ack" and row["venue_offer_id"] in mirrors
                  and classes[row["attempt_id"]] == "reflected"
                  and mirrors[row["venue_offer_id"]].present_in_latest_accepted_snapshot}
    assert ours == acked_live, (ours, acked_live)
    live_mirrors = {i for i, m in mirrors.items() if m.present_in_latest_accepted_snapshot}
    assert ours | set(final.classification["foreign"]) == live_mirrors
    # 2. legacy attempts not settled by the final snapshot <-> seeded unresolved, same kind.
    settled = set(final.classification["settled"]) | set(final.classification["reflected"])
    not_settled = {aid: row.outcome_kind for aid, row in legacy_attempts.items()
                   if str(aid) not in settled}
    unresolved = {row["attempt_id"]: row["outcome"] for row in seeded
                  if classes[row["attempt_id"]] == "unresolved"}
    kinds = {"acknowledged": "ack", "rejected": "rejected", "unknown": "unknown",
             "not_sent": "not_sent"}
    assert {aid: kinds[str(kind)] for aid, kind in not_settled.items()} == unresolved
    # 3. legacy open uncertainties <-> ledger open uncertainties.
    assert {(u.symbol, u.subject_id) for u in open_ledger} == open_legacy
    # 5. every seeded attempt resolves to a legacy attempt or a legacy claim.
    for row in seeded:
        provenance = row["seed_provenance"]
        if provenance["legacy"] == "submission_attempt":
            assert UUID(provenance["attempt_id"]) == row["attempt_id"] in legacy_attempts
        else:
            assert (provenance["cid"], provenance["execution_decision_id"]) in claims


async def _attempts(env: BotEnv) -> list[dict[str, Any]]:
    async with env.factory() as session:
        rows = await session.execute(
            select(SubmissionAttemptJournalRow, TransportOutcomeJournalRow.kind,
                   TransportOutcomeJournalRow.venue_offer_id)
            .join(TransportOutcomeJournalRow,
                  TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id))
        return [
            {"attempt_id": a.attempt_id, "attempt_seq": a.attempt_seq,
             "execution_decision_id": a.execution_decision_id, "outcome": kind,
             "policy_revision_id": a.policy_revision_id, "seed_provenance": a.seed_provenance,
             "normalized_payload": a.normalized_payload, "venue_offer_id": venue_offer_id}
            for a, kind, venue_offer_id in rows.tuples()
        ]


__all__ = ["Legacy", "Scope", "halt_moves", "run_legacy", "stop"]


class AllowCancel(AllowGuard):
    """The guard chain is not under test; it admits a cancel like a submit."""

    async def evaluate_cancel(self, decision: Any, context: Any) -> Any:
        return await self.evaluate(decision, context)


class CancellingVenue:
    """The gate's inner executor for a cancel: records what reaches the venue."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def submit(self, ready: Any, ctx: Any, *, cid: Any, reservation_ref: Any) -> Any:
        raise AssertionError("no submit in this test")

    async def cancel(self, *, venue_offer_id: str, signal_correlation_id: UUID,
                     account_id: str, ctx: Any) -> None:
        self.cancelled.append(venue_offer_id)


async def test_a_seeded_claim_only_offer_is_cancellable_by_the_ledger(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    """R1-1: the claim-only offer's seeded attempt carries the terms the ledger cancel needs
    (rate and period from the claim's decision, checked against the observed offer), so after
    the switch the ledger can cancel or reprice it like any managed offer (放貸全自動)."""
    env = bot_env
    env.venue.wallets = [["funding", "UST", "5000", 0, "5000"]]
    daemon = await env.build()
    await env.boot(daemon, T0)
    claim_decision = await claim_only_offer(env, at=T0 + 5_000)
    env.venue.offers = [offer_row(7006, CLAIM, CLAIM, T0 + 5_500, T0 + 5_500)]
    await env.tick(daemon, T0 + 20_000)
    assert daemon.periodic_reconcile._non_accepted == 0
    await stop(daemon)

    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0, lines
    (claim,) = [row for row in await _attempts(env)
                if row["execution_decision_id"] == claim_decision]
    assert claim["policy_revision_id"] is None and claim["seed_provenance"]["legacy"] == "offer_claim"
    await flip_epoch(env, at=SEED_AT + 1_000)
    boot_as_epoch(env, monkeypatch)
    ledger = await env.build()
    await env.boot(ledger, SEED_AT + 20_000)
    assert ledger.periodic_reconcile._non_accepted == 0

    gate = ledger.command_gate
    venue = CancellingVenue()
    gate._inner, gate._safety_evaluator = venue, AllowCancel()
    await gate.cancel(venue_offer_id="7006", signal_correlation_id=uuid4(),
                      account_id=str(SCOPE.exchange_account_id), ctx=env.ctx())
    assert venue.cancelled == ["7006"]
    # Checked last, so a seed without these terms fails at the cancel itself.
    assert claim["normalized_payload"] == {
        "symbol": "fUST", "amount": str(CLAIM), "rate": "0.0001", "period": 2, "type": "LIMIT",
        "flags": {"raw": 0}}  # type and flags as the legacy snapshot observed them
