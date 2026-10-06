"""``CommandJournal``: authorize, outcomes and cancel admission on the ledger stack."""

from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.modules.ledger import (
    Authorized,
    CancelAdmitted,
    CapitalAvailable,
    CapitalBlocked,
    CommandOutcome,
    CommandRefused,
    OutcomeAlreadyRecorded,
)

from .stacks import NOW, SCOPE

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

def _reason(result: object) -> str:
    assert isinstance(result, CommandRefused), result
    return result.reason


async def _guard(session) -> None:
    assert session.in_transaction()


async def _authorize(stack, amount="100", *, token=None, guard=_guard):
    """Authorize and commit one submit; returns (attempt, result)."""
    token = token or await stack.token()
    async with stack.factory.begin() as session:
        attempt = await stack.attempt(session, amount)
        result = await stack.journal.authorize(
            session, SCOPE, attempt, token, now_ms=NOW, locked_guard=guard
        )
    return attempt, result


async def _ready(stack, available="1000") -> None:
    await stack.policy()
    await stack.snapshot(available)


OUTCOMES = [
    ("ack", "m-1", None),
    ("rejected", None, "venue_rejected"),
    ("not_sent", None, "local_pre_transport"),
    ("unknown", None, "timeout"),
]


@pytest.mark.parametrize(("kind", "offer", "reason"), OUTCOMES)
async def test_authorize_then_outcome_reads_back_and_moves_the_ports(
    port_stack, kind, offer, reason
) -> None:
    await _ready(port_stack)
    attempt, result = await _authorize(port_stack, "200")
    assert isinstance(result, Authorized) and result.attempt_id == attempt.attempt_id
    charged = await port_stack.capital.read(port_stack.capital_scope(), now_ms=NOW)
    assert isinstance(charged, CapitalAvailable)
    assert charged.snapshot.unreflected_commitments == Decimal("200")  # held before any outcome
    assert await port_stack.journal.read_back_outcome(SCOPE, attempt.attempt_id) is None

    outcome = CommandOutcome(kind, offer, reason, NOW, {})
    await port_stack.journal.record_outcome(SCOPE, attempt.attempt_id, outcome)
    assert await port_stack.journal.read_back_outcome(SCOPE, attempt.attempt_id) == outcome

    after = await port_stack.capital.read(port_stack.capital_scope(), now_ms=NOW)
    unknown = kind == "unknown"
    assert await port_stack.uncertainties.has_open(None, SCOPE, "fUST") is unknown
    if unknown:
        assert isinstance(after, CapitalBlocked) and after.reason == "execution_unknown"
        return
    assert isinstance(after, CapitalAvailable)
    held = Decimal("200") if kind == "ack" else Decimal("0")  # a failed submit releases
    assert after.snapshot.unreflected_commitments == held
    assert after.budget.spendable == Decimal("900") - held


async def test_second_outcome_raises_with_the_stored_one(port_stack) -> None:
    await _ready(port_stack)
    attempt, _ = await _authorize(port_stack)
    first = CommandOutcome("ack", "m-1", None, NOW, {})
    await port_stack.journal.record_outcome(SCOPE, attempt.attempt_id, first)
    for second in (first, CommandOutcome("unknown", None, "timeout", NOW + 1, {})):
        with pytest.raises(OutcomeAlreadyRecorded) as error:
            await port_stack.journal.record_outcome(SCOPE, attempt.attempt_id, second)
        assert error.value.stored == first
    assert await port_stack.journal.read_back_outcome(SCOPE, attempt.attempt_id) == first


async def test_budget_exceeded_is_refused_and_writes_nothing(port_stack) -> None:
    await _ready(port_stack)  # spendable 900
    before = await port_stack.written()
    _, result = await _authorize(port_stack, "950")
    assert _reason(result) == "insufficient_deployable_funds"
    assert await port_stack.written() == before
    assert not await port_stack.uncertainties.has_open(None, SCOPE, "fUST")


async def test_the_whole_budget_is_authorized_once(port_stack) -> None:
    await _ready(port_stack)
    _, first = await _authorize(port_stack, "900")
    assert isinstance(first, Authorized)
    _, second = await _authorize(port_stack, "1")
    assert _reason(second) == "insufficient_deployable_funds"


async def test_policy_change_after_the_decision_is_refused_and_writes_nothing(port_stack) -> None:
    await _ready(port_stack)
    token = await port_stack.token()
    async with port_stack.factory() as session:
        attempt = await port_stack.attempt(session, "100")
        await session.commit()
    await port_stack.policy(reserve="200")  # revision 2
    before = await port_stack.written()
    async with port_stack.factory.begin() as session:
        result = await port_stack.journal.authorize(
            session, SCOPE, attempt, token, now_ms=NOW, locked_guard=_guard
        )
    assert _reason(result) == "capital_policy_revision_changed"
    assert await port_stack.written() == before


async def test_a_raising_guard_writes_nothing(port_stack) -> None:
    await _ready(port_stack)
    token = await port_stack.token()
    before = await port_stack.written()

    async def refuse(session) -> None:
        raise RuntimeError("guard refused")

    with pytest.raises(RuntimeError, match="guard refused"):
        await _authorize(port_stack, token=token, guard=refuse)
    assert await port_stack.written() == before


async def test_guard_runs_once_inside_the_transaction(port_stack) -> None:
    await _ready(port_stack)
    calls = []

    async def guard(session) -> None:
        assert session.in_transaction()
        calls.append(session)

    _, result = await _authorize(port_stack, guard=guard)
    assert isinstance(result, Authorized) and len(calls) == 1


async def test_stale_token_is_retried_once(port_stack) -> None:
    """3c2 ruling 6: the ledger re-reads capital inside its lock and succeeds when the
    budget still allows."""
    await _ready(port_stack)
    stale = await port_stack.token()
    await port_stack.snapshot("1000")  # a newer accepted snapshot since the decision
    before = await port_stack.written()
    calls = []

    async def guard(session) -> None:
        calls.append(session)

    _, result = await _authorize(port_stack, "100", token=stale, guard=guard)
    assert isinstance(result, Authorized) and len(calls) == 1
    assert await port_stack.written() != before


async def test_cancel_of_a_live_managed_offer_is_admitted(port_stack) -> None:
    await _ready(port_stack)
    attempt, _ = await _authorize(port_stack, "200")
    await port_stack.journal.record_outcome(
        SCOPE, attempt.attempt_id, CommandOutcome("ack", "m-1", None, NOW, {})
    )
    seen = []

    async def guard(session, admission) -> None:
        assert session.in_transaction()
        seen.append(admission)

    async with port_stack.factory.begin() as session:
        admitted = await port_stack.journal.admit_cancel(
            session, SCOPE, "m-1", now_ms=NOW, locked_guard=guard
        )
    assert isinstance(admitted, CancelAdmitted) and seen == [admitted]
    assert admitted.provenance.venue_offer_id == "m-1"
    assert admitted.provenance.symbol == "fUST"
    assert admitted.provenance.decision_id == attempt.execution_decision_id
    assert admitted.provenance.cell_id == "a30"
    assert (admitted.amount, admitted.rate, admitted.period_days) == (
        Decimal("200"), Decimal("0.0001"), 2,
    )


async def test_cancel_of_an_unknown_offer_is_refused(port_stack) -> None:
    await _ready(port_stack)

    async def never(session, admission) -> None:
        raise AssertionError("guard must not run on a refusal")

    async with port_stack.factory.begin() as session:
        refused = await port_stack.journal.admit_cancel(
            session, SCOPE, "nobody", now_ms=NOW, locked_guard=never
        )
    assert refused == CommandRefused("cancel_provenance_missing")


async def test_cancel_is_refused_while_the_symbol_has_an_open_uncertainty(port_stack) -> None:
    await _ready(port_stack)
    attempt, _ = await _authorize(port_stack, "200")
    await port_stack.journal.record_outcome(
        SCOPE, attempt.attempt_id, CommandOutcome("ack", "m-1", None, NOW, {})
    )
    await port_stack.unknown("50")

    async def never(session, admission) -> None:
        raise AssertionError("guard must not run on a refusal")

    async with port_stack.factory.begin() as session:
        refused = await port_stack.journal.admit_cancel(
            session, SCOPE, "m-1", now_ms=NOW, locked_guard=never
        )
    assert refused == CommandRefused("cancel_provenance_uncertain")
