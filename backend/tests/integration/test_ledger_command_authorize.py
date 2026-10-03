"""S1-3e2 ledger ``CommandJournal.authorize``: in-lock budget, derived basis, retry once.

Needs a real policy and accepted basis, so it runs on the capital-reader ``Book``
rather than the hand-seeded clone of ``test_ledger_authorization.py``.

Mutations (apply one at a time, run this file, revert):

1. retry on ``capital_policy_revision_changed``: ``test_policy_change_is_final_and_not_retried``.
2. retry on ``query_pending``: ``test_pending_query_is_final_and_not_retried``.
3. zero retries: ``test_stale_clock_is_retried_once_with_fresh_evidence``.
4. retry loops: ``test_a_second_stale_answer_is_final``.
5. retry skips the budget check: ``test_retry_rereads_the_budget``.
6. first pass skips the budget check: ``test_first_pass_budget_refusal_writes_nothing``.
7. ``locked_guard`` runs twice: ``test_stale_clock_is_retried_once_with_fresh_evidence``.
8. basis from the latest basis, not the token's query: ``test_pending_query_is_final_and_not_retried``,
   ``test_a_newer_accepted_query_is_retried_on_its_own_basis``.
9. evidence ``retried`` always false: ``test_stale_clock_is_retried_once_with_fresh_evidence``.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from bfx_funding_bot.modules.ledger import (
    Authorized,
    CommandAttempt,
    CommandRefused,
    encode_basis_token,
)
from bfx_funding_bot.modules.ledger._internal import capital_reader, journal
from bfx_funding_bot.modules.ledger.tables import (
    CapitalCommandClockRow,
    SubmissionAttemptJournalRow,
)
from bfx_funding_bot.modules.ledger.wiring import build_command_journal

from .test_ledger_capital_reader import (
    SCOPE,
    _view,
    book,  # noqa: F401 - fixture re-export
    ledger_db,  # noqa: F401 - fixture dependency
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW, MAX_AGE = 100, 1000


@pytest.fixture
def port(book):  # noqa: F811
    return build_command_journal(book.factory, max_snapshot_age_ms=MAX_AGE)


class Guard:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, session) -> None:
        assert session.in_transaction()
        self.calls += 1


async def _ready(book) -> None:  # noqa: F811
    await book.policy("fUST")  # reserve 100
    assert await book.accept() == "accepted"  # 1000 available: spendable 900


async def _decide(book, amount: str = "100"):  # noqa: F811
    """What the gate holds at decision time: (attempt without a session row yet, token)."""
    read = await book.read(now=NOW, max_age=MAX_AGE)
    applied = _view(read).applied
    token = encode_basis_token(read.query_id, read.clock_revision)
    decision_id = f"authorize-{uuid4()}"
    attempt = CommandAttempt(
        uuid4(), decision_id, "fUST", {"symbol": "fUST", "amount": amount},
        Decimal(amount), NOW, applied.revision, applied.digest, applied.revision_id,
        cell_id="a30",
    )
    return attempt, token


async def _authorize(book, port, attempt, token, guard):  # noqa: F811
    """One command transaction; the decision row exists as the gate wrote it earlier."""
    async with book.factory.begin() as session:
        await session.execute(
            text(
                "INSERT INTO execution_decisions(decision_id, account_id, exchange_account_id, "
                "deployment_environment, reconcile_id, cell_id, symbol, signal_correlation_id, "
                "outcome, signal_rate, amount_usdt, duration_days, model_evidence, safety_result, "
                "execution_policy, service_version, config_hash, occurred_at_ms, recorded_at_ms) "
                "VALUES (:d, 'account', :a, 'ci', 'r', 'a30', 'fUST', :d, 'submitted', 0, :amount, "
                "2, '{}', '{}', 'policy', 'test', 'hash', 0, 0)"
            ),
            {"d": attempt.execution_decision_id, "a": str(SCOPE.exchange_account_id),
             "amount": attempt.amount},
        )
        return await port.authorize(
            session, SCOPE, attempt, token, now_ms=NOW, locked_guard=guard
        )


async def _written(book) -> tuple[int, int]:  # noqa: F811
    async with book.factory() as session:
        return (
            await session.scalar(select(func.count()).select_from(SubmissionAttemptJournalRow)),
            await session.scalar(select(CapitalCommandClockRow.revision)),
        )


async def _evidence(book, attempt):  # noqa: F811
    async with book.factory() as session:
        row = await session.get(SubmissionAttemptJournalRow, attempt.attempt_id)
        assert row is not None
        return row.authorization_evidence, row.basis_id


@pytest.fixture
def reads(monkeypatch):
    """Every locked capital read the journal makes."""
    calls = []
    real = capital_reader.read_capital_locked

    async def spy(*args, **kwargs):
        calls.append(args[1])
        return await real(*args, **kwargs)

    monkeypatch.setattr(capital_reader, "read_capital_locked", spy)
    return calls


async def test_evidence_of_a_first_pass_names_the_in_lock_facts(book, port) -> None:  # noqa: F811
    await _ready(book)
    attempt, token = await _decide(book)
    guard = Guard()
    result = await _authorize(book, port, attempt, token, guard)
    assert isinstance(result, Authorized) and guard.calls == 1
    evidence, basis_id = await _evidence(book, attempt)
    assert basis_id == book.basis_id
    assert evidence == {
        "basis_token": token, "basis_id": str(book.basis_id),
        "max_new_offer": "900", "retried": False,
    }


async def test_stale_clock_is_retried_once_with_fresh_evidence(book, port, reads) -> None:  # noqa: F811
    await _ready(book)
    attempt, stale = await _decide(book)
    await book.attempt("100", outcome="ack", venue_offer_id="other")  # a command since the decision
    current = await book.read(now=NOW, max_age=MAX_AGE)
    fresh = encode_basis_token(current.query_id, current.clock_revision)
    before = await _written(book)
    reads.clear()
    guard = Guard()
    result = await _authorize(book, port, attempt, stale, guard)
    assert isinstance(result, Authorized)
    assert guard.calls == 1  # the guard runs only on the passing path
    assert len(reads) == 1  # the stale clock needs no read; only the retry re-reads, once
    evidence, basis_id = await _evidence(book, attempt)
    assert fresh != stale and basis_id == book.basis_id
    assert evidence == {
        "basis_token": fresh, "basis_id": str(book.basis_id),
        "max_new_offer": "800", "retried": True,
    }
    assert (await _written(book))[0] == before[0] + 1


async def test_a_newer_accepted_query_is_retried_on_its_own_basis(book, port) -> None:  # noqa: F811
    await _ready(book)
    attempt, stale = await _decide(book)
    old_basis = book.basis_id
    assert await book.accept() == "accepted"
    assert book.basis_id != old_basis
    guard = Guard()
    result = await _authorize(book, port, attempt, stale, guard)
    assert isinstance(result, Authorized) and guard.calls == 1
    evidence, basis_id = await _evidence(book, attempt)
    assert basis_id == book.basis_id and evidence is not None
    assert evidence["basis_id"] == str(book.basis_id) and evidence["retried"] is True


async def test_retry_rereads_the_budget(book, port) -> None:  # noqa: F811
    await _ready(book)
    attempt, stale = await _decide(book, "100")
    await book.attempt("850", outcome="ack", venue_offer_id="other")  # 50 left
    before = await _written(book)
    guard = Guard()
    result = await _authorize(book, port, attempt, stale, guard)
    assert result == CommandRefused("insufficient_deployable_funds")
    assert guard.calls == 0 and await _written(book) == before


async def test_first_pass_budget_refusal_writes_nothing(book, port, reads) -> None:  # noqa: F811
    await _ready(book)
    attempt, token = await _decide(book, "950")  # 900 spendable
    before = await _written(book)
    guard = Guard()
    result = await _authorize(book, port, attempt, token, guard)
    assert result == CommandRefused("insufficient_deployable_funds")
    assert guard.calls == 0 and await _written(book) == before
    assert len(reads) == 1  # a budget refusal is final, not retried


async def test_non_positive_amount_is_refused(book, port) -> None:  # noqa: F811
    await _ready(book)
    attempt, token = await _decide(book, "0")
    result = await _authorize(book, port, attempt, token, Guard())
    assert result == CommandRefused("intent_amount_conflict")


async def test_blocked_capital_refuses_with_its_reason(book, port) -> None:  # noqa: F811
    await _ready(book)
    attempt, _ = await _decide(book, "10")
    await book.attempt("10", outcome="unknown")  # blocks the symbol after the decision
    current = await book.read(now=NOW, max_age=MAX_AGE)
    token = encode_basis_token(current.query_id, current.clock_revision)
    before = await _written(book)
    result = await _authorize(book, port, attempt, token, Guard())
    assert result == CommandRefused("execution_unknown")
    assert await _written(book) == before


async def test_policy_change_is_final_and_not_retried(book, port, reads) -> None:  # noqa: F811
    await _ready(book)
    attempt, token = await _decide(book)
    await book.policy("fUST", reserve="200")  # a newer revision since the decision
    before = await _written(book)
    reads.clear()
    guard = Guard()
    result = await _authorize(book, port, attempt, token, guard)
    assert result == CommandRefused("capital_policy_revision_changed")
    assert len(reads) == 1 and guard.calls == 0 and await _written(book) == before


async def test_policy_change_wins_over_the_new_policys_budget(book, port) -> None:  # noqa: F811
    await _ready(book)
    attempt, token = await _decide(book)
    await book.policy("fUST", reserve="1000000")  # the new revision leaves no budget
    result = await _authorize(book, port, attempt, token, Guard())
    # The budget is the decision's policy's question; a new revision is reported as such.
    assert result == CommandRefused("capital_policy_revision_changed")


async def test_policy_change_with_a_stale_clock_is_final_after_the_retry_read(
    book, port, reads  # noqa: F811
) -> None:
    await _ready(book)
    attempt, stale = await _decide(book)
    await book.attempt("100", outcome="ack", venue_offer_id="other")
    await book.policy("fUST", reserve="200")
    reads.clear()
    result = await _authorize(book, port, attempt, stale, Guard())
    assert result == CommandRefused("capital_policy_revision_changed")
    assert len(reads) == 1  # the retry's re-read saw the new policy and stopped


async def test_pending_query_is_final_and_not_retried(book, port, reads) -> None:  # noqa: F811
    await _ready(book)
    attempt, _ = await _decide(book)
    handle = await book.begin()
    token = encode_basis_token(handle.query_id, handle.start_revision)
    before = await _written(book)
    reads.clear()
    guard = Guard()
    result = await _authorize(book, port, attempt, token, guard)
    assert result == CommandRefused("query_pending")  # not the previous query's basis
    assert not reads and guard.calls == 0 and await _written(book) == before


async def test_a_newer_pending_query_refuses_a_stale_token_without_a_second_pass(
    book, port, reads  # noqa: F811
) -> None:
    await _ready(book)
    attempt, stale = await _decide(book)
    await book.begin()  # the token's query is no longer the latest, and the latest has no basis
    before = await _written(book)
    reads.clear()
    guard = Guard()
    result = await _authorize(book, port, attempt, stale, guard)
    assert result == CommandRefused("query_pending")
    assert len(reads) == 1 and guard.calls == 0 and await _written(book) == before


async def test_malformed_token_is_final(book, port, reads) -> None:  # noqa: F811
    await _ready(book)
    attempt, _ = await _decide(book)
    reads.clear()
    result = await _authorize(book, port, attempt, "1", Guard())
    assert result == CommandRefused("capital_snapshot_changed") and not reads


async def test_a_second_stale_answer_is_final(book, port, reads, monkeypatch) -> None:  # noqa: F811
    await _ready(book)
    attempt, token = await _decide(book)
    resolved = []

    async def always_stale(*args, **kwargs):
        resolved.append(args[2])
        return journal._STALE

    monkeypatch.setattr(journal, "_resolve_token", always_stale)
    before = await _written(book)
    guard = Guard()
    result = await _authorize(book, port, attempt, token, guard)
    assert result == CommandRefused("capital_snapshot_changed")
    assert len(resolved) == 2 and resolved[0] == token  # one retry, with the re-read's token
    assert len(reads) == 1 and guard.calls == 0 and await _written(book) == before


async def test_attempt_without_a_cell_is_refused(book, port, reads) -> None:  # noqa: F811
    await _ready(book)
    attempt, token = await _decide(book)
    reads.clear()
    result = await _authorize(book, port, replace(attempt, cell_id=None), token, Guard())
    assert result == CommandRefused("execution_audit_conflict") and not reads
