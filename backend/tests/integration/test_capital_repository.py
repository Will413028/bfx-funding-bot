"""Real repository tests; PG cases use independent connections, never venue I/O."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.capital_tables
import bfx_funding_bot.modules.execution.uncertainty_tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_policy import CapitalPolicy
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationIntent,
    SnapshotCoverage,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from tests.modules.execution.event_store.test_historical_claim_cycles import (
    seal_prefix_chain,
)


@pytest.fixture(params=["sqlite", pytest.param("pg", marks=pytest.mark.integration)])
def capital_engine(request):
    return request.getfixturevalue("pg_engine" if request.param == "pg" else "sqlite_engine")


@pytest_asyncio.fixture
async def capital_db(capital_engine):
    async with capital_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(capital_engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="capital-test"))
    return factory, account


def repository(account, environment="ci"):
    # Import in the test body: RED identifies the absent repository capability.
    from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
    return CapitalRepository(account_id=account, environment=environment, max_snapshot_age_ms=10000)


def intent(account, amount="200", cid=1, symbol="fUST"):
    decision_id = str(uuid4())
    signal = uuid4()
    event = ReservationIntent(
        symbol=symbol, cid=cid, signal_correlation_id=signal, account_id=str(account),
        is_simulated=True, execution_decision_id=decision_id, amount=Decimal(amount),
        occurred_at_ms=1100,
        submission_attempt=SubmissionAttemptPayload(
            execution_decision_id=decision_id, account_id=account, environment="ci",
            symbol=symbol, cid=cid, started_at_ms=1100,
            normalized_payload={"symbol": symbol, "amount": amount, "rate": "0.0001", "period": 2},
        ),
    )
    decision = ExecutionDecisionRow(
        decision_id=decision_id, account_id=str(account), exchange_account_id=account,
        deployment_environment="ci", reconcile_id="test", cell_id="a30", symbol=symbol,
        signal_correlation_id=str(signal), outcome="ready", signal_rate=Decimal("0.0001"),
        applied_rate=Decimal("0.0001"), amount_usdt=Decimal(amount), duration_days=2,
        model_evidence={}, safety_result={}, execution_policy="test", service_version="test",
        config_hash="test", occurred_at_ms=1100, recorded_at_ms=1100,
    )
    return event, decision


async def snapshot(factory, repo, available="1000", offers=(), credits=()):
    async with factory.begin() as session:
        fence = await repo.begin_snapshot(session, now_ms=1000)
    event = VenueSnapshotObserved(
        account_id=str(repo.account_id), environment=repo.environment,
        query_started_at_ms=1000, query_finished_at_ms=1050,
        offers=offers, credits=credits, wallet_available={"fUST": Decimal(available), "fUSD": Decimal("0")},
        coverage=SnapshotCoverage(True, True, True),
    )
    async with factory.begin() as session:
        result = await repo.accept_snapshot(session, fence=fence, event=event,
            confirmation=replace(event, query_started_at_ms=1050, query_finished_at_ms=1060,
                                 event_id=uuid4()), now_ms=1060)
        assert result.event_seq is not None
        return result.event_seq


async def setup_policy(factory, repo, reserve="100", fraction="1"):
    async with factory.begin() as session:
        return await repo.apply_policy(session, symbol="fUST", policy=CapitalPolicy(
            enabled=True, reserve_amount=Decimal(reserve), max_cell_fraction=Decimal(fraction),
        ), expected_revision=0, source={"operator": "test"})


async def authorize(factory, repo, policy, seq, amount="200", cid=1):
    event, decision = intent(repo.account_id, amount, cid)
    async with factory.begin() as session:
        return await repo.authorize_and_append_intent(
            session, intent=event, decision=decision, expected_revision=policy.revision,
            expected_digest=policy.digest, expected_snapshot_seq=seq, now_ms=1100,
            locked_guard=simulated_guard,
        )


async def simulated_guard(session):
    assert session.in_transaction()


@pytest.mark.asyncio
async def test_applied_policy_cas_and_unavailable_fail_closed(capital_db):
    factory, account = capital_db
    repo = repository(account)
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="policy_unavailable"):
            await repo.read_applied(session, symbol="fUST")
    first = await setup_policy(factory, repo)
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="revision_changed"):
            await repo.apply_policy(session, symbol="fUST", policy=CapitalPolicy(enabled=False),
                                    expected_revision=0, source={})
        assert (await repo.read_applied(session, symbol="fUST")) == first


@pytest.mark.asyncio
async def test_reflected_commitment_is_not_charged_twice_and_survives_restart(capital_db):
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        before = await repository(account).read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert before.snapshot.unreflected_commitments == Decimal("200")
        assert before.budget.spendable == Decimal("700")
        row = await session.get(EventLogRow, result.event_seq)
        assert row.payload["capital_authorization"]["snapshot_seq"] == seq
        await repo.writer.append(session, ReservationClaimed(
            symbol="fUST", cid=1, signal_correlation_id=result.intent.signal_correlation_id,
            account_id=str(account), is_simulated=True, amount=Decimal("200"), venue_offer_id="offer-1",
            reservation_ref=replace(result.intent.reservation_ref, venue_offer_id="offer-1"),
            occurred_at_ms=1100,
        ))
    offer = VenueOfferObservation("offer-1", "fUST", Decimal("200"), Decimal("200"), Decimal("0.0001"), 2,
                                  "active", 1000, 1100)
    await snapshot(factory, repo, "800", offers=(offer,))
    async with factory.begin() as session:
        after = await repository(account).read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert after.snapshot.unreflected_commitments == Decimal("0")
        assert after.budget.spendable == Decimal("700")
        assert after.snapshot.total_capital == Decimal("1000")
        assert after.snapshot.cell_exposure == Decimal("200")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_independent_pg_sessions_only_one_reservation_fits(pg_session_factory):
    factory = pg_session_factory
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="race"))
    repo = repository(account)
    policy = await setup_policy(factory, repo, reserve="0")
    seq = await snapshot(factory, repo, "700")
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    ready = asyncio.Barrier(2)

    async def compete(cid):
        await ready.wait()
        try:
            await authorize(factory, repository(account), policy, seq, "500", cid)
            return "reserved"
        except CapitalBlockedError:
            return "blocked"

    assert sorted(await asyncio.gather(compete(1), compete(2))) == ["blocked", "reserved"]
    async with factory.begin() as session:
        state = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert state.snapshot.unreflected_commitments == Decimal("500")
        assert await session.scalar(select(func.count()).select_from(ExecutionDecisionRow)) == 1


@pytest.mark.asyncio
async def test_stable_observation_required_before_capital_acceptance(capital_db):
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)
    async with factory.begin() as session:
        fence = await repo.begin_snapshot(session, now_ms=1000)
    event = VenueSnapshotObserved(account_id=str(account), environment="ci",
        query_started_at_ms=1000, query_finished_at_ms=1050, offers=(), credits=(),
        wallet_available={"fUST": Decimal("1000")}, coverage=SnapshotCoverage(True, True, True))
    changed = replace(event, wallet_available={"fUST": Decimal("800")}, event_id=uuid4(),
                      query_started_at_ms=1050, query_finished_at_ms=1060)
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="snapshot_unstable"):
            await repo.accept_snapshot(session, fence=fence, event=event,
                                       confirmation=changed, now_ms=1060)
        assert await session.scalar(select(func.count()).select_from(EventLogRow)) == 0


@pytest.mark.asyncio
async def test_u_counts_for_each_cell_but_once_in_total(capital_db):
    from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo, "0", "0.70")
    credit = VenueCreditObservation("c1", "fUST", Decimal("300"), Decimal("0.0001"), 2, "active")
    await snapshot(factory, repo, "700", credits=(credit, credit))
    async with factory.begin() as session:
        for cell in ("a30", "p2"):
            view = await repo.read_capital(session, symbol="fUST", cell_id=cell, now_ms=1100)
            assert view.snapshot.total_capital == Decimal("1000")
            assert view.snapshot.cell_exposure == Decimal("300")
            assert view.budget.max_new_offer == Decimal("400")
            assert view.unattributed_credit_exposure == Decimal("300")
            assert "every cell" in view.attribution["credit_attribution"]


@pytest.mark.asyncio
async def test_changed_command_during_snapshot_fetch_rejected(capital_db):
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        fence = await repo.begin_snapshot(session, now_ms=1000)
    # A legacy/external writer can still race this fetch; it shares the writer
    # lock but has not yet adopted the Task3 capital boundary.
    event_intent, decision = intent(account)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        await repo.writer.append(session, event_intent)
    event = VenueSnapshotObserved(account_id=str(account), environment="ci",
        query_started_at_ms=1000, query_finished_at_ms=1050, offers=(), credits=(),
        wallet_available={"fUST": Decimal("1000")}, coverage=SnapshotCoverage(True, True, True))
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="snapshot_command_fence_changed"):
            await repo.accept_snapshot(session, fence=fence, event=event,
                confirmation=replace(event, query_started_at_ms=1050, query_finished_at_ms=1060,
                                     event_id=uuid4()), now_ms=1060)


@pytest.mark.asyncio
async def test_locked_guard_failure_leaves_no_intent_or_decision(capital_db):
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    event, decision = intent(account)
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError

    async def halted(session):
        assert session.in_transaction()
        raise CapitalBlockedError("halted")

    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="halted"):
            await repo.authorize_and_append_intent(session, intent=event, decision=decision,
                expected_revision=policy.revision, expected_digest=policy.digest,
                expected_snapshot_seq=seq, now_ms=1100, locked_guard=halted)
        assert await session.scalar(select(func.count()).select_from(ExecutionDecisionRow)) == 0


@pytest.mark.asyncio
async def test_new_unfinished_query_invalidates_old_snapshot(capital_db):
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        await repo.begin_snapshot(session, now_ms=1100)
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="snapshot_query_pending"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)


@pytest.mark.asyncio
@pytest.mark.parametrize("unstable", [False, True])
async def test_boot_recovery_stable_capital_ingestion(capital_db, unstable):
    from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)

    class Venue:
        wallet_calls = 0

        async def get_active_funding_offers(self, **kwargs):
            return []

        async def get_active_funding_loans(self, **kwargs):
            # Lent but not yet drawn into a position; none in this fixture.
            return []

        async def get_active_funding_credits(self, **kwargs):
            return []

        async def get_funding_available_all(self, **kwargs):
            self.wallet_calls += 1
            return {"fUST": Decimal("800" if unstable and self.wallet_calls == 2 else "1000")}

    class Bus:
        async def publish(self, event):
            pass

    recovery = BootRecovery(store=PostgresEventStore(deployment_environment="ci"),
        session_factory=factory, auth_rest=Venue(), account_ctx=AccountContext(
            str(account), Credentials("fixture", "fixture"), Decimal("0")),
        deployment_environment="ci", bus=Bus(), symbols=["fUST"], is_simulated=True,
        clock=lambda: 1100, capital_repository=repo)
    if unstable:
        with pytest.raises(CapitalBlockedError, match="snapshot_unstable"):
            await recovery.run()
        async with factory.begin() as session:
            # Reconcile evidence survives a refused capital observation, so
            # ordinary quarantine/recovery can still progress while submit blocks.
            assert await session.scalar(select(func.count()).select_from(EventLogRow)) == 1
            with pytest.raises(CapitalBlockedError, match="snapshot_unavailable"):
                await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
    else:
        await recovery.run()
        async with factory.begin() as session:
            view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
            assert view.budget.spendable == Decimal("900")
            row = await session.get(EventLogRow, view.snapshot_seq)
            assert row.payload["capital_command_fence"] == 0
            assert row.payload["capital_confirmation"]["wallet_available"] == {"fUST": "1000"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["not_sent", "rejected", "unknown"])
async def test_typed_outcome_releases_or_blocks_commitment(capital_db, kind):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.events import ReservationFailed, ReservationUnknown
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    event_type = ReservationUnknown if kind == "unknown" else ReservationFailed
    async with factory.begin() as session:
        await repo.writer.append(session, event_type(
            symbol="fUST", cid=1, account_id=str(account), is_simulated=True,
            signal_correlation_id=result.intent.signal_correlation_id,
            reservation_ref=result.intent.reservation_ref, amount=Decimal("200"),
            reason="local_pre_transport" if kind == "not_sent" else "test-outcome",
            occurred_at_ms=1150,
        ))
    async with factory.begin() as session:
        if kind == "unknown":
            with pytest.raises(CapitalBlockedError, match="execution_unknown"):
                await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1200)
        else:
            view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1200)
            assert view.snapshot.unreflected_commitments == Decimal("0")
            assert view.budget.spendable == Decimal("900")


@pytest.mark.asyncio
async def test_partial_fill_does_not_add_original_reservation(capital_db):
    from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        await repo.writer.append(session, ReservationClaimed(
            symbol="fUST", cid=1, signal_correlation_id=result.intent.signal_correlation_id,
            account_id=str(account), is_simulated=True, amount=Decimal("200"), venue_offer_id="offer-1",
            reservation_ref=replace(result.intent.reservation_ref, venue_offer_id="offer-1"),
            occurred_at_ms=1100))
    offer = VenueOfferObservation("offer-1", "fUST", Decimal("200"), Decimal("50"), Decimal("0.0001"), 2, "active", 1000, 1100)
    credit = VenueCreditObservation("c1", "fUST", Decimal("150"), Decimal("0.0001"), 2, "active")
    await snapshot(factory, repo, "800", offers=(offer,), credits=(credit,))
    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1200)
        assert view.snapshot.total_capital == Decimal("1000")
        assert view.snapshot.cell_exposure == Decimal("200")
        assert view.snapshot.unreflected_commitments == 0


@pytest.mark.asyncio
async def test_policy_and_snapshot_fences_isolation_and_rollback(capital_db):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="policy_unavailable"):
            await repository(account, "other").read_applied(session, symbol="fUST")
        with pytest.raises(CapitalBlockedError, match="policy_unavailable"):
            await repo.read_applied(session, symbol="fUSD")
        newer = await repo.apply_policy(session, symbol="fUST", policy=policy.policy,
                                       expected_revision=1, source={"test": "revision-2"})
    with pytest.raises(CapitalBlockedError, match="revision_changed"):
        await authorize(factory, repo, policy, seq)
    with pytest.raises(CapitalBlockedError, match="snapshot_changed"):
        await authorize(factory, repo, newer, seq + 99)
    event, decision = intent(account)
    with pytest.raises(RuntimeError, match="crash"):
        async with factory.begin() as session:
            await repo.authorize_and_append_intent(session, intent=event, decision=decision,
                expected_revision=2, expected_digest=newer.digest, expected_snapshot_seq=seq,
                now_ms=1100, locked_guard=simulated_guard)
            raise RuntimeError("crash before commit")
    async with factory.begin() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionDecisionRow)) == 0
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert view.snapshot.unreflected_commitments == 0


@pytest.mark.asyncio
async def test_resolved_unknown_audit_does_not_permanently_block(capital_db):
    from bfx_funding_bot.modules.execution.events import UncertaintyMarkedNotAccepted
    from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
    from tests.modules.execution.event_store.test_uncertainty_resolution_events import (
        ACCOUNT,
        _open_unknown,
        _snapshot,
    )
    factory, _ = capital_db
    unknown_id = await _open_unknown(factory)
    # The pre-existing fixture realm is intentionally explicit.
    from tests.modules.execution.event_store.test_uncertainty_resolution_events import ENV
    repo = repository(ACCOUNT, ENV)
    async with factory.begin() as session:
        observed = await repo.writer.append(session, _snapshot(finished=5, offer=False))
        await repo.writer.append(session, UncertaintyMarkedNotAccepted(
            uncertainty_id=unknown_id, account_id=str(ACCOUNT), environment=ENV, symbol="fUST",
            kind="submit_outcome_unknown", reconcile_event_seq=observed.event_seq,
            resolved_by_operator_id="test", resolution_reason="zero complete history candidates",
            resolution_evidence={"reconcile_event_seq": observed.event_seq,
                "query_started_at_ms": 4, "query_finished_at_ms": 5, "candidate_count": 0},
            candidate_count=0, occurred_at_ms=6))
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert view.snapshot.unreflected_commitments == 0
        assert (await session.scalar(select(SubmissionAttemptRow))).outcome_kind == "unknown"


@pytest.mark.asyncio
async def test_resolved_unknowns_add_no_work_to_an_authorization_read(capital_db, monkeypatch):
    """D3a resolves UNKNOWNs routinely and each keeps outcome_kind='unknown' for
    audit. Acceptance proved them settled; a read must not re-prove them one by
    one, or its work grows with every resolution ever made (AGENTS.local
    capital-authority trap). Counts the work, not the queries."""
    from bfx_funding_bot.modules.execution.events import UncertaintyMarkedNotAccepted
    from tests.modules.execution.event_store.test_uncertainty_resolution_events import (
        ACCOUNT,
        ENV,
        _open_unknown,
        _snapshot,
    )
    factory, _ = capital_db
    unknown_id = await _open_unknown(factory)
    repo = repository(ACCOUNT, ENV)
    async with factory.begin() as session:
        observed = await repo.writer.append(session, _snapshot(finished=5, offer=False))
        await repo.writer.append(session, UncertaintyMarkedNotAccepted(
            uncertainty_id=unknown_id, account_id=str(ACCOUNT), environment=ENV, symbol="fUST",
            kind="submit_outcome_unknown", reconcile_event_seq=observed.event_seq,
            resolved_by_operator_id="test", resolution_reason="zero complete history candidates",
            resolution_evidence={"reconcile_event_seq": observed.event_seq,
                "query_started_at_ms": 4, "query_finished_at_ms": 5, "candidate_count": 0},
            candidate_count=0, occurred_at_ms=6))
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    calls = 0
    real = type(repo)._effective_outcome

    async def counting(self, session, attempt):
        nonlocal calls
        calls += 1
        return await real(self, session, attempt)

    monkeypatch.setattr(type(repo), "_effective_outcome", counting)
    async with factory.begin() as session:
        await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
    assert calls == 0


@pytest.mark.asyncio
async def test_corrupt_snapshot_classification_fails_closed(capital_db):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    async with factory.begin() as session:
        row = await session.get(CapitalSnapshotRow, seq)
        forged = dict(row.classification)
        forged["symbols"] = {"fUST": {"available": "10000", "offered": "0", "credits": "0",
                                     "unattributed_credits": "0", "cells": {}}}
        row.classification = forged
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="snapshot_evidence_conflict"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)


@pytest.mark.asyncio
async def test_pending_intent_gap_replayed_before_capital_read(capital_db):
    from bfx_funding_bot.modules.execution.event_store.serialization import serialize_event
    from bfx_funding_bot.modules.execution.event_store.tables import ProjectionHeadRow
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    # Simulate the durable log ahead of the projection cursor. No venue call.
    event, decision = intent(account)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        session.add(EventLogRow(account_id=str(account), exchange_account_id=account,
            deployment_environment="ci", event_type="RESERVATION_INTENT", cid=1,
            event_id=event.event_id, schema_version=event.schema_version,
            payload=serialize_event(event), occurred_at_ms=1100))
    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert view.snapshot.unreflected_commitments == Decimal("200")
        head = await session.scalar(select(ProjectionHeadRow))
        assert head.last_event_seq > seq
    # Restart/replay never creates another attempt or charges the same identity twice.
    async with factory.begin() as session:
        view = await repository(account).read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert view.budget.spendable == Decimal("700")
    with pytest.raises(Exception, match="attempt_already_committed"):
        async with factory.begin() as session:
            await repo.authorize_and_append_intent(session, intent=event, decision=decision,
                expected_revision=policy.revision, expected_digest=policy.digest,
                expected_snapshot_seq=seq, now_ms=1100, locked_guard=simulated_guard)


@pytest.mark.asyncio
async def test_terminal_history_proves_first_snapshot_reflection(capital_db):
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        await repo.writer.append(session, ReservationClaimed(
            symbol="fUST", cid=1, signal_correlation_id=result.intent.signal_correlation_id,
            account_id=str(account), is_simulated=True, amount=Decimal("200"), venue_offer_id="offer-1",
            reservation_ref=replace(result.intent.reservation_ref, venue_offer_id="offer-1"),
            occurred_at_ms=1100))
    from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
    terminal = VenueOfferObservation("offer-1", "fUST", Decimal("200"), Decimal("0"), Decimal("0.0001"), 2,
                                     "executed", 1100, 1150)
    async with factory.begin() as session:
        fence = await repo.begin_snapshot(session, now_ms=1200)
    event = VenueSnapshotObserved(account_id=str(account), environment="ci",
        query_started_at_ms=1200, query_finished_at_ms=1250, offers=(),
        credits=(VenueCreditObservation("c1", "fUST", Decimal("200"), Decimal("0.0001"), 2, "active"),),
        wallet_available={"fUST": Decimal("800")}, offer_history=(terminal,),
        coverage=SnapshotCoverage(True, True, True, offer_history_complete=True,
            offer_history_pages=1, offer_history_start_ms=1000, offer_history_end_ms=1200))
    async with factory.begin() as session:
        await repo.accept_snapshot(session, fence=fence, event=event,
            confirmation=replace(event, query_started_at_ms=1250, query_finished_at_ms=1260), now_ms=1260)
    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1300)
        assert view.snapshot.unreflected_commitments == 0
        assert view.budget.spendable == Decimal("700")
        assert view.snapshot.cell_exposure == Decimal("200")


@pytest.mark.asyncio
async def test_historical_terminal_intent_is_not_a_permanent_block(capital_db):
    from bfx_funding_bot.modules.execution.events import ReservationFailed
    factory, account = capital_db
    repo = repository(account)
    event, decision = intent(account)
    legacy = replace(event, submission_attempt=None)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        await repo.writer.append(session, legacy)
        await repo.writer.append(session, ReservationFailed(symbol="fUST", cid=1,
            account_id=str(account), is_simulated=True, amount=Decimal("200"),
            signal_correlation_id=event.signal_correlation_id, reservation_ref=event.reservation_ref,
            reason="local_pre_transport", occurred_at_ms=1100))
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1300)
        assert view.snapshot.unreflected_commitments == 0
        assert view.budget.spendable == Decimal("900")


@pytest.mark.asyncio
async def test_unknown_schema_stale_snapshot_and_other_account_are_blocked(capital_db):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRevisionRow
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    await snapshot(factory, repo)
    other = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=other, venue="bitfinex", label="other"))
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="policy_unavailable"):
            await repository(other).read_applied(session, symbol="fUST")
        with pytest.raises(CapitalBlockedError, match="snapshot_stale"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=20000)
        row = await session.get(CapitalPolicyRevisionRow, policy.revision_id)
        row.schema_version = 999
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="invalid_policy_schema"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_writer_lock_serializes_contending_authorization(pg_session_factory):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    factory = pg_session_factory
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="lock-proof"))
    repo = repository(account)
    policy = await setup_policy(factory, repo, "0")
    seq = await snapshot(factory, repo, "700")
    waiter_ready = asyncio.Event()
    waiter_pid = None

    async def second():
        nonlocal waiter_pid
        async with factory.begin() as session:
            waiter_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            waiter_ready.set()
            event, decision = intent(account, "500", 2)
            try:
                await repo.authorize_and_append_intent(session, intent=event, decision=decision,
                    expected_revision=policy.revision, expected_digest=policy.digest,
                    expected_snapshot_seq=seq, now_ms=1100, locked_guard=simulated_guard)
                return "reserved"
            except CapitalBlockedError:
                return "blocked"

    async with factory.begin() as first_session:
        event, decision = intent(account, "500", 1)
        await repo.authorize_and_append_intent(first_session, intent=event, decision=decision,
            expected_revision=policy.revision, expected_digest=policy.digest,
            expected_snapshot_seq=seq, now_ms=1100, locked_guard=simulated_guard)
        waiter = asyncio.create_task(second())
        await waiter_ready.wait()
        async with factory() as observer:
            for _ in range(100):
                blocked = await observer.scalar(text(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": waiter_pid})
                await observer.rollback()  # refresh pg_stat_activity statistics snapshot
                if blocked:
                    break
                await asyncio.sleep(0.01)
            else:
                waiter.cancel()
                pytest.fail("second independent connection never contended for account lock")
        assert not waiter.done()
    assert await asyncio.wait_for(waiter, 5) == "blocked"


@pytest.mark.asyncio
async def test_outcome_without_terminal_event_cannot_release_capital(capital_db):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        attempt = await session.scalar(select(SubmissionAttemptRow))
        attempt.outcome_kind = "rejected"
        attempt.completed_at_ms = 1200
        attempt.outcome_reason = "forged without terminal event"
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="attempt_outcome_evidence"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1200)


@pytest.mark.asyncio
async def test_unknown_active_credit_status_does_not_authorize(capital_db):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)
    credit = VenueCreditObservation("c1", "fUST", Decimal("200"), Decimal("0.001"), 2,
                                   "future-unknown-status")
    with pytest.raises(CapitalBlockedError, match="snapshot_invalid_active_credit"):
        await snapshot(factory, repo, "800", credits=(credit,))


@pytest.mark.parametrize("fault", ["missing", "account", "environment", "symbol", "cid", "payload"])
async def test_current_cursor_cannot_hide_durable_commitment(capital_db, fault):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.event_store.tables import ProjectionHeadRow
    from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        assert (view.snapshot.unreflected_commitments, view.budget.spendable) == (200, 700)
        head = await session.scalar(select(ProjectionHeadRow))
        assert head.last_event_seq == result.event_seq
        attempt = await session.get(SubmissionAttemptRow, result.intent.submission_attempt.attempt_id)
        if fault == "missing":
            await session.delete(attempt)
        elif fault == "account":
            other = ExchangeAccount(id=uuid4(), venue="bitfinex", label="other")
            session.add(other)
            await session.flush()
            attempt.exchange_account_id = other.id
        elif fault == "environment":
            attempt.deployment_environment = "elsewhere"
        elif fault == "symbol":
            attempt.symbol = "fUSD"
        elif fault == "cid":
            attempt.cid = 999
        else:
            attempt.normalized_payload = dict(attempt.normalized_payload, period=30)
    # Cursor remains current: gap replay cannot silently repair this corruption.
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match=r"attempt_.*(missing|conflict)"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
    with pytest.raises(CapitalBlockedError, match=r"attempt_.*(missing|conflict)"):
        await authorize(factory, repo, policy, seq, cid=2)
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match=r"attempt_.*(missing|conflict)"):
            await repo.begin_snapshot(session, now_ms=1200)
        assert await session.scalar(select(func.count()).select_from(EventLogRow).where(
            EventLogRow.event_type == "RESERVATION_INTENT")) == 1


async def seed_historical_cycles(factory, account, *, reused, terminal="ORDER_FILL", pending=False):
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows
    rows = historical_rows(first_amount="7", environment="ci")
    if not reused:
        rows = rows[:2 if pending else 3]
    next_signal = str(uuid4())
    for index, row in enumerate(rows):
        row.account_id = row.payload["account_id"] = str(account)
        row.exchange_account_id = account
        if index >= 3:
            row.payload["signal_correlation_id"] = next_signal
        if row.event_type == "ORDER_FILL":
            row.event_type = terminal
            row.payload["reason"] = "venue_cancel"
    async with factory.begin() as session:
        session.add_all(rows)
        await session.flush()
        await seal_prefix_chain(session)
        await PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
            session, account_id=str(account), deployment_environment="ci")
    return [row.event_seq for row in rows]


@pytest.mark.parametrize("reused", [False, True])
@pytest.mark.parametrize("terminal", ["ORDER_FILL", "RESERVATION_RELEASED"])
async def test_completed_historical_cycles_allow_capital(capital_db, reused, terminal):
    from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
    factory, account = capital_db
    repo = repository(account)
    sequences = await seed_historical_cycles(factory, account, reused=reused, terminal=terminal)
    async with factory.begin() as session:
        rows = (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_seq.in_(sequences)).order_by(EventLogRow.event_seq))).all()
        before = canonical_event_hash(rows)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    for _ in range(2):  # Recreate repository: no process-local cycle cache.
        async with factory.begin() as session:
            view = await repository(account).read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
            assert view.snapshot.unreflected_commitments == 0
            assert view.budget.spendable == 900
            rows = (await session.scalars(select(EventLogRow).where(
                EventLogRow.event_seq.in_(sequences)).order_by(EventLogRow.event_seq))).all()
            assert canonical_event_hash(rows) == before
    await authorize(factory, repo, policy, seq, cid=2)


@pytest.mark.parametrize("fault", ["partial_fill", "correlation", "venue", "row_cid", "scope"])
@pytest.mark.parametrize("terminal", ["ORDER_FILL", "RESERVATION_RELEASED"])
async def test_historical_terminal_requires_exact_cycle_evidence(capital_db, fault, terminal):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    factory, account = capital_db
    repo = repository(account)
    sequences = await seed_historical_cycles(factory, account, reused=False, terminal=terminal)
    async with factory.begin() as session:
        row = await session.get(EventLogRow, sequences[-1])
        payload = dict(row.payload)
        if fault == "partial_fill":
            payload.update(amount="2", size_usdt="2")
        elif fault == "correlation":
            payload["signal_correlation_id"] = str(uuid4())
        elif fault == "venue":
            row.venue_offer_id = payload["venue_offer_id"] = "unrelated"
        elif fault == "row_cid":
            row.cid = 999
        else:
            row.deployment_environment = "elsewhere"
        row.payload = payload
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="unclassifiable_legacy_intent"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)


async def test_historical_completion_after_query_fence_cannot_release_capital(capital_db):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows
    factory, account = capital_db
    repo = repository(account)
    await seed_historical_cycles(factory, account, reused=False, pending=True)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    terminal = historical_rows(first_amount="7", environment="ci")[2]
    terminal.account_id = terminal.payload["account_id"] = str(account)
    terminal.exchange_account_id = account
    async with factory.begin() as session:
        session.add(terminal)
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="unclassifiable_legacy_intent"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)


async def test_snapshot_acceptance_rechecks_complete_attempt_inventory(capital_db):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        await repo.writer.append(session, ReservationClaimed(
            symbol="fUST", cid=1, signal_correlation_id=result.intent.signal_correlation_id,
            account_id=str(account), is_simulated=True, amount=Decimal("200"), venue_offer_id="offer-1",
            reservation_ref=replace(result.intent.reservation_ref, venue_offer_id="offer-1"),
            occurred_at_ms=1100))
        fence = await repo.begin_snapshot(session, now_ms=1200)
    async with factory.begin() as session:
        attempt = await session.get(SubmissionAttemptRow, result.intent.submission_attempt.attempt_id)
        await session.delete(attempt)
    event = VenueSnapshotObserved(account_id=str(account), environment="ci",
        query_started_at_ms=1200, query_finished_at_ms=1250, offers=(), credits=(),
        wallet_available={"fUST": Decimal("1000")}, coverage=SnapshotCoverage(True, True, True))
    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="attempt_projection_missing"):
            await repo.accept_snapshot(session, fence=fence, event=event,
                confirmation=replace(event, query_started_at_ms=1250, query_finished_at_ms=1260),
                now_ms=1260)


@pytest.mark.parametrize("shared_venue", [False, True])
async def test_historical_cycles_validate_venue_ownership_across_cids(capital_db, shared_venue):
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows
    factory, account = capital_db
    repo = repository(account)
    await seed_historical_cycles(factory, account, reused=False)
    rows = historical_rows(environment="ci")[3:]
    for row in rows:
        row.account_id = row.payload["account_id"] = str(account)
        row.exchange_account_id = account
        row.cid = row.payload["cid"] = 82
    async with factory.begin() as session:
        session.add_all(rows)
        await session.flush()
        await seal_prefix_chain(session)
        await PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
            session, account_id=str(account), deployment_environment="ci")
        if shared_venue:
            for row in rows[1:]:
                row.venue_offer_id = "old-a"
                row.payload = dict(row.payload, venue_offer_id="old-a")
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        if shared_venue:
            with pytest.raises(CapitalBlockedError, match="unclassifiable_legacy_intent"):
                await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        else:
            view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
            assert view.budget.spendable == 900


@pytest.mark.parametrize("cycle_pairs", [2, 64])
async def test_historical_capital_read_has_bounded_scope_queries_and_validation(
    capital_db, capital_engine, monkeypatch, cycle_pairs,
):
    from unittest.mock import Mock

    from sqlalchemy import event as sql_event

    import bfx_funding_bot.modules.execution.capital_repository as module
    from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows

    factory, account = capital_db
    repo = repository(account)
    rows = []
    for index in range(cycle_pairs):
        for row in historical_rows(environment="ci"):
            row.account_id = row.payload["account_id"] = str(account)
            row.exchange_account_id = account
            row.cid = row.payload["cid"] = 1000 + index
            if row.venue_offer_id:
                row.venue_offer_id = row.payload["venue_offer_id"] = f"{index}-{row.venue_offer_id}"
            rows.append(row)
    async with factory.begin() as session:
        session.add_all(rows)
        await session.flush()
        await seal_prefix_chain(session)
        await module.PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
            session, account_id=str(account), deployment_environment="ci",
        )
    await setup_policy(factory, repo)
    validator = Mock(wraps=module.historical_claim_reset_sequences)
    monkeypatch.setattr(module, "historical_claim_reset_sequences", validator)
    await snapshot(factory, repo)
    accepted = validator.call_count
    assert accepted >= 1, "acceptance must run the full-scope validation"
    statements = []

    def record_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    sql_event.listen(capital_engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        for _ in (1, 2):
            statements.clear()
            async with factory.begin() as session:
                view = await repository(account).read_capital(
                    session, symbol="fUST", cell_id="a30", now_ms=1100,
                )
                assert view.budget.spendable == 900
            # Full-scope validation is renewed for each acceptance, where the fence
            # is set, and never repeated per read or per legacy intent. A read that
            # renewed it could not be bounded, because the scope is all of history.
            assert validator.call_count == accepted
            assert len(statements) < 30, statements
    finally:
        sql_event.remove(capital_engine.sync_engine, "before_cursor_execute", record_sql)


async def test_capital_read_work_does_not_grow_with_history(capital_db, monkeypatch):
    """Authorization work must be bounded by construction, not by a wall-clock timeout.

    The sibling bounded-scope test counts SQL *statements*, which stayed flat while the
    work per read grew with every intent ever recorded — so it never discriminated. Count
    the work itself: deserialization is what consumed the guard budget in production on
    2026-09-20 (3,703 RESERVATION_INTENT rows, `capital_policy eval_timeout >2.0s`, every
    offer blocked regardless of halt state, with no signal that anything had degraded).

    Pins guarantee: authorization is bounded. Completeness is pinned separately by the
    prefix-hash and historical-cycle tests; this one must fail on unbounded work alone.
    """
    import bfx_funding_bot.modules.execution.capital_repository as module
    from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows

    factory, small_account = capital_db
    large_account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=large_account, venue="bitfinex", label="capital-large"))

    async def seed(account, cycle_pairs):
        rows = []
        for index in range(cycle_pairs):
            for row in historical_rows(environment="ci"):
                row.account_id = row.payload["account_id"] = str(account)
                row.exchange_account_id = account
                row.cid = row.payload["cid"] = 1000 + index
                if row.venue_offer_id:
                    row.venue_offer_id = row.payload["venue_offer_id"] = f"{index}-{row.venue_offer_id}"
                rows.append(row)
        async with factory.begin() as session:
            session.add_all(rows)
            await session.flush()
            await seal_prefix_chain(session)
            await module.PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
                session, account_id=str(account), deployment_environment="ci",
            )
        repo = repository(account)
        await setup_policy(factory, repo)
        await snapshot(factory, repo)
        return repo

    async def deserializations(repo):
        calls = 0
        real = module.deserialize_stored_event

        def counting(row):
            nonlocal calls
            calls += 1
            return real(row)

        monkeypatch.setattr(module, "deserialize_stored_event", counting)
        try:
            async with factory.begin() as session:
                view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
                assert view.budget.spendable == 900
        finally:
            monkeypatch.setattr(module, "deserialize_stored_event", real)
        return calls

    small, large = 2, 64
    small_calls = await deserializations(await seed(small_account, small))
    large_calls = await deserializations(await seed(large_account, large))

    # History grew by (64-2)*6 = 372 events. A bounded read re-derives nothing from the
    # proven prefix, so its work must not track that growth.
    assert large_calls - small_calls <= 2, (
        f"read work grew with history: {small} pairs -> {small_calls} deserializations, "
        f"{large} pairs -> {large_calls}"
    )


async def test_snapshot_records_the_prefix_it_was_derived_from(capital_db):
    """Acceptance names the ledger prefix; a read checks that claim for free.

    Pins guarantee (2): completeness is proved by a content hash of the covered
    prefix, never by a cursor having advanced.
    """
    from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
    from bfx_funding_bot.modules.execution.event_store.tables import EventPrefixHashRow

    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)

    async with factory.begin() as session:
        row = await session.scalar(
            select(CapitalSnapshotRow).order_by(CapitalSnapshotRow.event_seq.desc()).limit(1)
        )
        # The chain lives beside the ledger: event_log is append-only and a trigger
        # refuses to have a capital-bearing row updated after insert.
        chain = await session.get(EventPrefixHashRow, row.event_seq)
        assert row.covered_prefix_hash is not None
        assert chain is not None and row.covered_prefix_hash == chain.prefix_hash
        # And the read accepts it.
        view = await repository(account).read_capital(
            session, symbol="fUST", cell_id="a30", now_ms=1100,
        )
        assert view.budget.spendable == 900


@pytest.mark.parametrize("fault", ["mismatch", "unproven"])
async def test_snapshot_whose_prefix_no_longer_matches_cannot_authorize(capital_db, fault):
    """A classification derived from a prefix that is no longer the ledger's blocks.

    NULL is unproven rather than absent: a snapshot accepted before the binding
    existed has made no checkable claim, so it cannot authorize either.
    """
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow

    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)

    async with factory.begin() as session:
        row = await session.scalar(
            select(CapitalSnapshotRow).order_by(CapitalSnapshotRow.event_seq.desc()).limit(1)
        )
        row.covered_prefix_hash = None if fault == "unproven" else "0" * 64
        await session.flush()

    async with factory.begin() as session:
        with pytest.raises(CapitalBlockedError, match="snapshot_prefix_diverged"):
            await repository(account).read_capital(
                session, symbol="fUST", cell_id="a30", now_ms=1100,
            )


@pytest.mark.parametrize("with_history", [False, True])
async def test_bounded_read_agrees_with_full_rederivation(capital_db, with_history):
    """The authorization read and the audit re-derivation must give one answer.

    Tautological while _read_capital delegates to _read_capital_full, and load
    bearing the moment it stops: the bounded path may skip re-deriving the proven
    prefix, but it may not reach a different conclusion about spendable capital.

    Pins guarantee (1): the bounded path still accounts for every commitment the
    full scan accounts for.
    """
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from tests.modules.execution.event_store.test_historical_claim_cycles import historical_rows

    factory, account = capital_db
    repo = repository(account)
    if with_history:
        rows = []
        for index in range(3):
            for row in historical_rows(environment="ci"):
                row.account_id = row.payload["account_id"] = str(account)
                row.exchange_account_id = account
                row.cid = row.payload["cid"] = 2000 + index
                if row.venue_offer_id:
                    row.venue_offer_id = row.payload["venue_offer_id"] = f"d{index}-{row.venue_offer_id}"
                rows.append(row)
        async with factory.begin() as session:
            session.add_all(rows)
            await session.flush()
            await seal_prefix_chain(session)
            await PostgresEventStore(deployment_environment="ci").rebuild_snapshot_from_log(
                session, account_id=str(account), deployment_environment="ci",
            )
    await setup_policy(factory, repo)
    await snapshot(factory, repo)

    async with factory.begin() as session:
        reader = repository(account)
        applied = await reader.read_applied(session, symbol="fUST")
        bounded = await reader._read_capital(
            session, symbol="fUST", cell_id="a30", now_ms=1100, applied=applied,
        )
        full = await reader._read_capital_full(
            session, symbol="fUST", cell_id="a30", now_ms=1100, applied=applied,
        )

    assert bounded.snapshot == full.snapshot
    assert bounded.budget == full.budget
    assert bounded.snapshot_seq == full.snapshot_seq
    assert bounded.applied == full.applied


@pytest.mark.asyncio
async def test_an_offer_that_fills_between_snapshots_is_accounted_for(capital_db):
    """The first live fill, 2026-09-23, reproduced with the venue's own shapes.

    The canary was acknowledged at 02:08:25 and filled on the spot, so by the
    next snapshot it was gone from the active book. Three things then hid it:
    offer history was only fetched for UNKNOWN submits, so none was fetched;
    had it been, the status "EXECUTED at 0.0148% (150.78)" did not read as
    terminal, which discards the whole history page; and the lent money sat
    under /funding/loans, which nothing read. The wallet's available balance
    had dropped and nothing on the books explained it, so every snapshot after
    it failed as unclassifiable_commitment and funding status went dark.
    """
    from bfx_funding_bot.external.bitfinex.auth_rest import (
        ActiveFundingCredit,
        ActiveFundingOffer,
        FundingOfferHistory,
        FundingOfferHistoryCoverage,
    )
    from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        await repo.writer.append(session, ReservationClaimed(
            symbol="fUST", cid=1, signal_correlation_id=result.intent.signal_correlation_id,
            account_id=str(account), is_simulated=True, amount=Decimal("200"), venue_offer_id="offer-1",
            reservation_ref=replace(result.intent.reservation_ref, venue_offer_id="offer-1"),
            occurred_at_ms=1100))

    class Venue:
        async def get_active_funding_offers(self, **kwargs):
            return []                                   # filled: not in the book

        async def get_active_funding_credits(self, **kwargs):
            return []                                   # not drawn into a position

        async def get_active_funding_loans(self, **kwargs):
            return [ActiveFundingCredit(credit_id="loan:61621685", symbol="fUST",
                amount=Decimal("200"), rate=0.0001, period_days=2, status="ACTIVE",
                mts_created=1150, mts_updated=1150)]

        async def get_funding_available_all(self, **kwargs):
            return {"fUST": Decimal("800")}             # 200 left the wallet

        async def get_funding_offer_history(self, *, ctx, start_ms, end_ms, symbol=None):
            assert start_ms <= 1100, "history must reach back to the acknowledged attempt"
            return FundingOfferHistory(
                offers=(ActiveFundingOffer(
                    venue_offer_id="offer-1", symbol="fUST", amount=Decimal("0"),
                    rate=0.0001, period_days=2, mts_created=1150,
                    status="EXECUTED at 0.0100% (200.0)",   # the venue's own wording
                    amount_original=Decimal("200"), mts_updated=1150, offer_type="LIMIT"),),
                coverage=FundingOfferHistoryCoverage(
                    requested_start_ms=start_ms, requested_end_ms=end_ms,
                    oldest_mts_created=1150, newest_mts_created=1150, pages=1, complete=True))

    class Bus:
        async def publish(self, event):
            pass

    recovery = BootRecovery(store=PostgresEventStore(deployment_environment="ci"),
        session_factory=factory, auth_rest=Venue(), account_ctx=AccountContext(
            str(account), Credentials("fixture", "fixture"), Decimal("0")),
        deployment_environment="ci", bus=Bus(), symbols=["fUST"], is_simulated=True,
        clock=lambda: 1200, capital_repository=repo)
    await recovery.run()

    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1250)
        assert view.snapshot.unreflected_commitments == 0
        assert view.snapshot.cell_exposure == Decimal("200")   # the loan is on the books
        assert view.budget.spendable == Decimal("700")          # 800 available less 100 reserve


@pytest.mark.asyncio
async def test_foreign_offer_is_counted_out_of_managed_exposure(capital_db):
    """D2: an active offer no claim or attempt traces to is not an error. Its
    300 already left the wallet's available; leaving it out of offered is what
    makes the total 700, and no cell carries it."""
    from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
    factory, account = capital_db
    repo = repository(account)
    await setup_policy(factory, repo, reserve="0")
    manual = VenueOfferObservation("manual-1", "fUST", Decimal("300"), Decimal("300"),
                                   Decimal("0.0001"), 2, "active", 1000, 1000)
    seq = await snapshot(factory, repo, "700", offers=(manual,))
    async with factory.begin() as session:
        view = await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        row = await session.get(CapitalSnapshotRow, seq)
    assert row.authorization_blocked_reason is None
    assert row.classification["foreign"] == {"manual-1": {"symbol": "fUST", "amount": "300"}}
    assert row.classification["symbols"]["fUST"]["foreign"] == "300"
    assert row.classification["symbols"]["fUST"]["offered"] == "0"
    assert view.snapshot.total_capital == Decimal("700")
    assert view.snapshot.cell_exposure == Decimal("0")
    assert view.budget.spendable == Decimal("700")


@pytest.mark.asyncio
async def test_open_unknown_withholds_only_its_symbol_and_does_not_stop_acceptance(capital_db):
    """Point 4 of T2: an UNKNOWN on fUST no longer refuses every snapshot of the
    account. The snapshot is accepted with the attempt recorded against fUST;
    fUST reads refuse, another symbol reads normally."""
    from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
    from bfx_funding_bot.modules.execution.capital_tables import CapitalSnapshotRow
    from bfx_funding_bot.modules.execution.events import ReservationUnknown
    factory, account = capital_db
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    async with factory.begin() as session:
        await repo.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                expected_revision=0, source={"operator": "test"})
    seq = await snapshot(factory, repo)
    result = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        await repo.writer.append(session, ReservationUnknown(
            symbol="fUST", cid=1, account_id=str(account), is_simulated=True,
            signal_correlation_id=result.intent.signal_correlation_id,
            reservation_ref=result.intent.reservation_ref, amount=Decimal("200"),
            reason="test-outcome", occurred_at_ms=1150,
        ))
    later = await snapshot(factory, repo)
    async with factory.begin() as session:
        row = await session.get(CapitalSnapshotRow, later)
        assert row.classification["unresolved"] == {
            str(result.intent.submission_attempt.attempt_id): "fUST"}
        with pytest.raises(CapitalBlockedError, match="execution_unknown"):
            await repo.read_capital(session, symbol="fUST", cell_id="a30", now_ms=1100)
        other = await repo.read_capital(session, symbol="fUSD", cell_id="a30", now_ms=1100)
    assert other.budget.reason == "policy_disabled"
