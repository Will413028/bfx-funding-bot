"""Synthetic UNKNOWN cycles sourced from prod seq 9502→9539 and 9849→9859 (labels only)."""

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.events import UncertaintyMarkedNotAccepted
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.trading import Available, Blocked
from tests.integration.test_capital_repository import intent, simulated_guard, snapshot
from tests.integration.test_trading_shadow_candidate import (
    candidate_db,  # noqa: F401
    outcome,
)
from tests.integration.test_trading_shadow_comparison import comparison
from tests.modules.execution.event_store.test_uncertainty_resolution_events import _snapshot

pytestmark = pytest.mark.integration


async def authorize(factory, repo, policy, seq, *, cid):
    """Like the shared helper, but with a complete venue payload.

    UNKNOWN resolution only matches attempts whose normalized payload carries
    type and flags (unknown_matching.attempt_from_row), as live submits do.
    """
    event, decision = intent(repo.account_id, cid=cid)
    attempt = event.submission_attempt
    assert attempt is not None
    event = replace(event, submission_attempt=replace(
        attempt,
        normalized_payload={**attempt.normalized_payload, "type": "LIMIT", "flags": 0},
        payload_sha256=None,
    ))
    async with factory.begin() as session:
        return await repo.authorize_and_append_intent(
            session, intent=event, decision=decision, expected_revision=policy.revision,
            expected_digest=policy.digest, expected_snapshot_seq=seq, now_ms=1100,
            locked_guard=simulated_guard,
        )


async def test_two_consecutive_unknown_not_accepted_cycles(candidate_db):  # noqa: F811
    factory, repo, policy, seq = candidate_db
    # candidate_db already applies setup_policy and accepts the initial snapshot.
    decisions, attempts, cids, correlations = set(), set(), set(), set()

    for cid in (1, 2):
        # seq is the previous cycle's final acceptance; no state is reset.
        admitted = await authorize(factory, repo, policy, seq, cid=cid)
        opening = admitted.intent
        assert opening.submission_attempt is not None
        for seen, identity in (
            (decisions, opening.execution_decision_id),
            (attempts, opening.submission_attempt.attempt_id),
            (cids, opening.cid),
            (correlations, opening.signal_correlation_id),
        ):
            assert identity is not None and identity not in seen
            seen.add(identity)
        await outcome(factory, repo, opening, "unknown")

        # 1. The open uncertainty blocks both arms.
        opened = await comparison(factory, repo)
        assert opened.status == "equal", opened
        for arm in (opened.candidate, opened.baseline):
            assert isinstance(arm, Blocked)
            assert arm.reason == "execution_unknown"
        assert opened.classifications == ("unknown_open",)

        # Capture the unresolved attempt in the accepted basis before resolution.
        await snapshot(factory, repo)
        async with factory.begin() as session:
            identity = await session.scalar(
                select(ExecutionUncertaintyRow.uncertainty_id).where(
                    ExecutionUncertaintyRow.exchange_account_id == repo.account_id,
                    ExecutionUncertaintyRow.attempt_id == opening.submission_attempt.attempt_id,
                    ExecutionUncertaintyRow.state == "open",
                )
            )
            assert identity is not None
            observed = await repo.writer.append(session, replace(
                _snapshot(finished=1201),
                account_id=str(repo.account_id), environment=repo.environment,
            ))
            await repo.writer.append(session, UncertaintyMarkedNotAccepted(
                uncertainty_id=identity, account_id=str(repo.account_id),
                environment=repo.environment, symbol="fUST", kind="submit_outcome_unknown",
                reconcile_event_seq=observed.event_seq,
                resolved_by_operator_id="test", resolution_reason="proven",
                resolution_evidence={
                    "reconcile_event_seq": observed.event_seq,
                    "query_started_at_ms": 1200,
                    "query_finished_at_ms": 1201,
                    "candidate_count": 0,
                },
                occurred_at_ms=1202, candidate_count=0,
            ))

        # 2. NOT_ACCEPTED alone cannot release the accepted unresolved commitment.
        # The store only accepts the resolution against an observation newer than
        # the accepted snapshot, so both arms block on that observation first.
        resolved = await comparison(factory, repo)
        assert resolved.status == "equal", resolved
        for arm in (resolved.candidate, resolved.baseline):
            assert isinstance(arm, Blocked)
            assert arm.reason == "snapshot_superseded_by_unfenced_observation"
        assert resolved.classifications == ()

        # 3. A fresh query, confirmation and acceptance release the commitment.
        seq = await snapshot(factory, repo)
        accepted = await comparison(factory, repo)
        assert accepted.status == "equal", accepted
        for arm in (accepted.candidate, accepted.baseline):
            assert isinstance(arm, Available)
            assert arm.view.snapshot_seq == seq
            assert arm.view.snapshot.unreflected_commitments == Decimal("0")
        assert accepted.classifications == ()
