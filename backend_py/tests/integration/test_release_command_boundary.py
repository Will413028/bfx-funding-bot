"""Real account command gate and persistence; only the HTTP venue is replaced."""
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.release_tables import ReleaseSessionRow
from bfx_funding_bot.modules.execution.safety.hard_guards import ManualKillGuard
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from tests.external.bitfinex.test_funding_rules import FixedRules
from tests.integration.test_capital_command_boundary import boundary
from tests.integration.test_capital_repository import capital_db as capital_db
from tests.integration.test_capital_repository import capital_engine as capital_engine
from tests.integration.test_release_sessions import prepared


@pytest.mark.asyncio
async def test_same_daemon_gate_consumes_session_and_attempt_before_mock_venue(capital_db):
    factory, account = capital_db
    gate, venue, ready, ctx, capital, halt = await boundary(factory, account)
    repo, sid, binding, _ = await prepared(factory, account)
    token = object()
    from bfx_funding_bot.modules.execution.release_worker import ReleaseCommandAuthority
    async def binding_reader(session):
        return binding
    authority = ReleaseCommandAuthority(repo=repo, capital=capital, binding_reader=binding_reader,
                                        ownership=lambda: _true(), authority_reader=lambda s, u: _true(),
                                        preflight=lambda s, r: _true(), clock=lambda: 1100)
    gate.release_authority = authority
    gate._safety_evaluator = ManualKillGuard(halt_store=halt, canary_halt_authorization=token)
    async with factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, ready.decision_id)
        row.strategy, row.amount_usdt = "mean_reversion", Decimal("150")
    ready = replace(ready, decision=ready.decision.model_copy(update={"offer_amount_usdt": 150}))
    ctx = replace(ctx, release_session_id=sid, canary_halt_authorization=token)
    original = venue.submit

    async def submit(*args, **kwargs):
        async with factory() as session:
            row = await session.get(ReleaseSessionRow, sid)
            assert row.state == "consumed"
            assert row.decision_id == ready.decision_id
            assert await session.get(SubmissionAttemptRow, row.attempt_id) is not None
        return await original(*args, **kwargs)

    venue.submit = submit
    await gate.submit(ready, ctx)
    assert len(venue.received) == 1
    with pytest.raises(Exception, match="permit_already_consumed"):
        await gate.submit(ready, ctx)
    assert len(venue.received) == 1
    assert (await halt.current()).halted
    async with factory() as session:
        attempts = (await session.scalars(select(SubmissionAttemptRow))).all()
        assert len(attempts) == 1


async def _true():
    return True


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("promotion_fault", [None, "revoked", "runtime", "policy", "epoch", "ownership_during_readiness", "runtime_during_readiness"])
async def test_ack_two_fences_validation_and_delayed_promotion(pg_session_factory, promotion_fault):
    from uuid import uuid4

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
    from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
    from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
    from bfx_funding_bot.modules.execution.release_session import ReleaseBlocked
    from bfx_funding_bot.modules.execution.release_worker import (
        ReleaseCommandAuthority,
        ReleaseWorker,
    )
    from bfx_funding_bot.modules.marketfeed.daemon import (
        CanaryProfile,
        assert_release_observation,
        collect_canary_readiness,
    )
    from scripts.run_canary_preflight import (
        CanaryAttemptSelector,
        _derive_canary_evidence_from_durable_rows,
    )
    factory, account = pg_session_factory, uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="fixture release lifecycle"))
    gate, venue, ready, ctx, capital, halt = await boundary(factory, account)
    repo, sid, expected_binding, epoch = await prepared(factory, account)
    binding = dict(expected_binding)
    now = 1100
    authorized = True
    owned = True

    async def current_binding(session):
        return binding

    async def operator(session, user):
        return authorized

    async def ownership():
        return owned

    async def observe(session, row):
        nonlocal owned, binding
        profile = CanaryProfile(account, "ci", "fUST", "a30", "mean_reversion", Decimal("150"), Decimal("250"), 300)
        proof = await _derive_canary_evidence_from_durable_rows(session=session, profile=profile,
            projector_version="execution-state-v1", now_ms=now,
            claim=CanaryAttemptSelector(str(row.permit_id), str(row.attempt_id), row.decision_id))
        readiness = await collect_canary_readiness(session, account_id=account, environment="ci",
            now_ms=now, after_event_seq=proof.outcome_event_seq, minimum_snapshot_count=2)
        assert_release_observation(profile=profile, evidence=proof, readiness=readiness)
        if row.requested_action == "promote":
            if promotion_fault == "ownership_during_readiness":
                owned = False
            elif promotion_fault == "runtime_during_readiness":
                binding = {**binding, "release_digest": "changed-during-readiness"}
        return proof

    authority = ReleaseCommandAuthority(repo=repo, capital=capital, binding_reader=current_binding,
        ownership=ownership, authority_reader=operator, preflight=lambda s, r: _true(), clock=lambda: now)
    token = object()
    gate.release_authority = authority
    gate._safety_evaluator = ManualKillGuard(halt_store=halt, canary_halt_authorization=token)
    async with factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, ready.decision_id)
        row.strategy, row.amount_usdt = "mean_reversion", Decimal("150")
    ready = replace(ready, decision=ready.decision.model_copy(update={"offer_amount_usdt": 150}))
    ctx = replace(ctx, release_session_id=sid, canary_halt_authorization=token)
    await gate.submit(ready, ctx)

    async def no_submit(command):
        pytest.fail("consumed session retried submit")

    worker = ReleaseWorker(authority=authority, halt_store=halt, funding_rules=FixedRules(),
        configured_cells=(("mean_reversion", "fUST", "a30"),), halt_authorization=token,
        planner=no_submit, observation=observe)
    await worker.tick()
    async with factory.begin() as session:
        assert (await repo.get(session, sid)).state == "observed"
        await repo.request_action(session, sid, action="validate", operator="operator", expected_revision=2, now_ms=now)
    await worker.tick()  # No post-outcome snapshots: not validated, still halted.
    async with factory() as session:
        assert (await repo.get(session, sid)).state == "observed"
    assert (await halt.current()).id == epoch
    for started in (6000, 6100):  # Authorization expired; read-only observation remains legal.
        async with factory.begin() as session:
            fence = await capital.repository.begin_snapshot(session, now_ms=started)
        event = VenueSnapshotObserved(account_id=str(account), environment="ci", query_started_at_ms=started,
            query_finished_at_ms=started + 10,
            offers=(VenueOfferObservation(venue_offer_id="101", symbol="fUST", amount_original=Decimal("150"),
                amount_remaining=Decimal("150"), rate=Decimal("0.0001"), period_days=2,
                status="ACTIVE", mts_created=1100, mts_updated=1100),),
            credits=(), wallet_available={"fUST": Decimal("850"), "fUSD": Decimal("0")},
            coverage=SnapshotCoverage(True, True, True))
        async with factory.begin() as session:
            await capital.repository.accept_snapshot(session, fence=fence, event=event,
                confirmation=replace(event, query_started_at_ms=started+11, query_finished_at_ms=started+20,
                    event_id=uuid4()), now_ms=started+20)
    now = 6200
    async with factory.begin() as session:
        await repo.request_action(session, sid, action="validate", operator="operator", expected_revision=3, now_ms=now)
    await worker.tick()
    async with factory.begin() as session:
        row = await repo.get(session, sid)
        assert row.state == "validated", row.reason
        await repo.request_action(session, sid, action="promote", operator="operator", expected_revision=4, now_ms=now)
    assert (await halt.current()).halted  # validated never auto-resumes
    if promotion_fault == "revoked":
        authorized = False
    elif promotion_fault in {"runtime", "policy"}:
        binding = {**binding, "release_digest" if promotion_fault == "runtime" else "policies": "changed"}
    elif promotion_fault == "epoch":
        await halt.set_halted(False, reason="fixture transition", actor="fixture")
        await halt.set_halted(True, reason="fixture new epoch", actor="fixture")
    await worker.tick()
    assert len(venue.received) == 1
    async with factory.begin() as session:
        row = await repo.get(session, sid)
        assert row.state == ("promoted" if promotion_fault is None else "blocked"), row.reason
        if promotion_fault is None:
            await authority.check_normal(session)
            original_check = authority.check_normal
            checked = 0
            async def revoke_after_cancel_admission(locked):
                nonlocal binding, checked
                await original_check(locked)
                checked += 1
                if checked == 1:
                    binding = {**binding, "release_digest": "changed-before-cancel-transport"}
            authority.check_normal = revoke_after_cancel_admission
            with pytest.raises(ReleaseBlocked, match="promotion_required"):
                await gate.cancel(venue_offer_id="101", signal_correlation_id=ready.decision.signal_correlation_id,
                    account_id=str(account), ctx=replace(ctx, release_session_id=None, canary_halt_authorization=None))
            assert len(venue.received) == 1
            authority.check_normal = original_check
            # Same stable identity supports a restarted authority; changed identity never does.
            binding = {**binding, "release_digest": "new-release"}
            with pytest.raises(ReleaseBlocked, match="promotion_required"):
                await authority.check_normal(session)
    assert (await halt.current()).halted is (promotion_fault is not None)
