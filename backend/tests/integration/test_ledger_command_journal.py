"""S1-3c2 cancel fences and owned outcome transactions on a migrated clone."""

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.ledger import (
    CancelAdmitted,
    CommandOutcome,
    CommandRefused,
    Outcome,
    OutcomeAlreadyRecorded,
    Quarantine,
)
from bfx_funding_bot.modules.ledger._internal.journal import record_attempt
from bfx_funding_bot.modules.ledger.wiring import build_command_journal

from .test_ledger_authorization import (
    _fresh_attempt,
    _state,
    factory,  # noqa: F401 - fixture re-export
    ledger_db,  # noqa: F401 - fixture dependency
    seeded,  # noqa: F401 - fixture dependency
)
from .test_ledger_journal import JOURNAL, SCOPE
from .test_ledger_schema_roles import _A, _O

pytestmark = pytest.mark.integration


async def _own_attempt(session, kind="ack", offer="cancel-me"):
    attempt = await _fresh_attempt(session)
    attempt = replace(attempt, normalized_payload={"amount": "1", "rate": "0.0001", "period": 2})
    await session.execute(text("UPDATE execution_decisions SET applied_rate=0.0001 WHERE decision_id=:d"),
                          {"d": attempt.execution_decision_id})
    await record_attempt(session, SCOPE, attempt)
    if kind is not None:
        await JOURNAL.record_outcome(session, SCOPE, Outcome(
            attempt.attempt_id, kind, offer if kind == "ack" else None,
            "timeout" if kind == "unknown" else None, 10, {},
        ))
    return attempt


async def _guard(session, admission):
    assert session.in_transaction()
    assert admission.provenance.venue_offer_id == "cancel-me"


@pytest.mark.asyncio
async def test_ack_only_cancel_bumps_once_and_rollback_restores_clock(factory):  # noqa: F811
    async with factory.begin() as session:
        attempt = await _own_attempt(session)
        before = await _state(session)
    port = build_command_journal(factory, max_snapshot_age_ms=1000)
    async with factory.begin() as session:
        result = await port.admit_cancel(session, SCOPE, "cancel-me", now_ms=20, locked_guard=_guard)
        assert isinstance(result, CancelAdmitted)
        assert result.provenance.attempt_id == attempt.attempt_id
        assert (result.amount, result.rate, result.period_days) == (Decimal(1), Decimal("0.0001"), 2)
        assert await _state(session) == (*before[:2], before[2] + 1)
        await session.rollback()
    async with factory.begin() as session:
        assert await _state(session) == before
        assert isinstance(await port.admit_cancel(
            session, SCOPE, "cancel-me", now_ms=20, locked_guard=_guard,
        ), CancelAdmitted)
    async with factory.begin() as session:
        assert await _state(session) == (*before[:2], before[2] + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "terminal", "unknown", "quarantine", "conflict"])
async def test_cancel_refusal_has_no_writes(factory, change):  # noqa: F811
    async with factory.begin() as session:
        await _own_attempt(session)
        if change == "unknown":
            await _own_attempt(session, kind="unknown")
        elif change == "quarantine":
            await JOURNAL.open_quarantine(session, SCOPE, Quarantine(uuid4(), "fUST", Decimal(1), 10, {}))
        elif change == "conflict":
            await _own_attempt(session)  # a distinct decision, same venue id
        elif change == "terminal":
            evidence_id = uuid4()
            await session.execute(text(
                "INSERT INTO ledger_observation_offer_history(id,observation_id,venue_offer_id,symbol,"
                "amount_original,amount_remaining,rate_observed,status,mts_created,terminal_kind,"
                "occurred_at_ms,raw) VALUES (:id,:o,'cancel-me','fUST',1,0,false,'active',0,'canceled',10,'{}')"
            ), {"id": evidence_id, "o": UUID(_O)})
            await session.execute(text(
                "INSERT INTO venue_offer_mirror(exchange_account_id,deployment_environment,venue_offer_id,"
                "symbol,amount_original,amount_remaining,rate_observed,status,mts_created,"
                "last_accepted_observation_id,present_in_latest_accepted_snapshot,terminal_evidence_id,terminal_kind) "
                "VALUES (:a,'ci','cancel-me','fUST',1,0,false,'active',0,:o,false,:id,'canceled')"
            ), {"a": UUID(_A), "o": UUID(_O), "id": evidence_id})
        before = await _state(session)
    calls = []

    async def never(session, admission):
        calls.append(admission)

    reason = ("cancel_provenance_uncertain" if change in {"unknown", "quarantine"}
              else "cancel_provenance_conflict" if change == "conflict" else "cancel_provenance_missing")
    async with factory.begin() as session:
        assert await build_command_journal(factory, max_snapshot_age_ms=1000).admit_cancel(
            session, SCOPE, "absent" if change == "missing" else "cancel-me",
            now_ms=20, locked_guard=never,
        ) == CommandRefused(reason)
        assert not calls
        assert await _state(session) == before


@pytest.mark.asyncio
async def test_cancel_guard_failure_can_be_caught_and_commits_nothing(factory):  # noqa: F811
    async with factory.begin() as session:
        await _own_attempt(session)
        before = await _state(session)

    async def refuse(session, admission):
        assert session.in_transaction()
        assert await _state(session) == before  # guard precedes the bump
        raise RuntimeError("guard refused")

    async with factory.begin() as session:
        with pytest.raises(RuntimeError, match="guard refused"):
            await build_command_journal(factory, max_snapshot_age_ms=1000).admit_cancel(
                session, SCOPE, "cancel-me", now_ms=20, locked_guard=refuse,
            )
        assert await _state(session) == before
    async with factory.begin() as session:
        assert await _state(session) == before


@pytest.mark.asyncio
async def test_command_outcome_owned_commit_duplicate_and_scoped_readback(factory):  # noqa: F811
    async with factory.begin() as session:
        attempt = await _own_attempt(session, kind=None)
        before = await _state(session)
    port = build_command_journal(factory, max_snapshot_age_ms=1000)
    assert await port.read_back_outcome(SCOPE, attempt.attempt_id) is None
    outcome = CommandOutcome("ack", "cancel-me", None, 10, {"test": True})
    await port.record_outcome(SCOPE, attempt.attempt_id, outcome)
    assert await port.read_back_outcome(SCOPE, attempt.attempt_id) == outcome
    for second in (outcome, replace(outcome, kind="unknown", venue_offer_id=None, reason="timeout")):
        with pytest.raises(OutcomeAlreadyRecorded) as error:
            await port.record_outcome(SCOPE, attempt.attempt_id, second)
        assert error.value.stored == outcome
    with pytest.raises(ValueError, match="scope mismatch"):
        await port.read_back_outcome(replace(SCOPE, deployment_environment="other"), attempt.attempt_id)
    async with factory.begin() as session:
        assert await _state(session) == (before[0], before[1] + 1, before[2] + 1)
