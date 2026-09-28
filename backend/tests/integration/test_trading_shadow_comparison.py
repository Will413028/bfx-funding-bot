"""PostgreSQL parity and read-only boundaries for S0-2b (requires Docker)."""

from decimal import Decimal

import pytest
from sqlalchemy import text, update

from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.capital_shadow_baseline import read_baseline
from bfx_funding_bot.modules.execution.event_store.tables import ProjectionHeadRow
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.execution.events import (
    ReservationUnknown,
    UncertaintyMarkedNotAccepted,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.trading import CapitalScope
from bfx_funding_bot.modules.trading_shadow._internal.comparison import compare_capital
from bfx_funding_bot.modules.trading_shadow.wiring import build_candidate_loader
from tests.integration.test_capital_repository import (
    authorize,
    intent,
    repository,
    setup_policy,
    snapshot,
)
from tests.integration.test_trading_shadow_candidate import (
    begin_read,
    outcome,
)
from tests.modules.execution.event_store.test_uncertainty_resolution_events import (
    ACCOUNT,
    ENV,
    _open_unknown,
    _snapshot,
)

pytestmark = pytest.mark.integration
pytest_plugins = ("tests.integration.test_trading_shadow_candidate",)


async def comparison(factory, repo, *, cell="a30", session=None, baseline=read_baseline):
    kwargs = {
        "scope": CapitalScope(repo.account_id, repo.environment, "fUST", cell),
        "now_ms": 2000,
        "max_snapshot_age_ms": 10000,
        "candidate_reader": build_candidate_loader(source_revision="integration"),
        "baseline_reader": baseline,
    }
    if session is not None:
        return await compare_capital(session, **kwargs)
    async with factory.begin() as reader:
        await begin_read(reader)
        return await compare_capital(reader, **kwargs)


@pytest.mark.parametrize("kind", ["pending", "acknowledged", "not_sent", "rejected", "unknown"])
async def test_tail_parity(candidate_db, kind):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    if kind != "pending":
        await outcome(factory, repo, admitted.intent, kind)
    result = await comparison(factory, repo)
    assert result.status == "equal", result
    assert result.candidate_input_digest and result.baseline_observation_digest
    assert result.baseline_input_digest is None and result.baseline_evidence_complete is False


async def test_partial_fill_and_multiple_cells(candidate_db):
    from bfx_funding_bot.modules.execution.event_store.entities import (
        VenueCreditObservation,
        VenueOfferObservation,
    )

    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    await outcome(factory, repo, admitted.intent, "acknowledged")
    offer = VenueOfferObservation(
        "offer-1", "fUST", Decimal("200"), Decimal("50"), Decimal("0.0001"), 2,
        "active", 1000, 1100,
    )
    credit = VenueCreditObservation("c1", "fUST", Decimal("150"), Decimal("0.0001"), 2, "active")
    await snapshot(factory, repo, "800", offers=(offer,), credits=(credit,))
    for cell in ("a30", "b50"):
        result = await comparison(factory, repo, cell=cell)
        assert result.status == "equal", (cell, result)


async def test_legacy_unknown_without_attempt(candidate_db):
    factory, repo, _, _ = candidate_db
    legacy, decision = intent(repo.account_id)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        await repo.writer.append(session, ReservationUnknown(
            symbol="fUST", cid=legacy.cid,
            signal_correlation_id=legacy.signal_correlation_id,
            account_id=str(repo.account_id), is_simulated=True,
            reservation_ref=legacy.reservation_ref, amount=legacy.amount,
            reason="legacy_timeout", occurred_at_ms=1200,
        ))
    result = await comparison(factory, repo)
    assert result.status == "equal", result


async def test_resolved_unknown_waiting_for_new_snapshot(pg_session_factory):
    factory = pg_session_factory
    identity = await _open_unknown(factory)
    repo = repository(ACCOUNT, ENV)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    async with factory.begin() as session:
        observed = await repo.writer.append(session, _snapshot(finished=1201))
        await repo.writer.append(session, UncertaintyMarkedNotAccepted(
            uncertainty_id=identity, account_id=str(ACCOUNT), environment=ENV,
            symbol="fUST", kind="submit_outcome_unknown",
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
    result = await comparison(factory, repo, cell="cell-1")
    assert result.status == "equal", result


async def test_repeatable_read_hides_second_connection_commit(candidate_db):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    async with factory.begin() as reader:
        await begin_read(reader)
        await reader.execute(text("SELECT 1 FROM event_log LIMIT 1"))
        first = await comparison(factory, repo, session=reader)
        await outcome(factory, repo, admitted.intent, "not_sent")
        second = await comparison(factory, repo, session=reader)
    assert first.status == second.status == "equal"
    assert first.candidate_input_digest == second.candidate_input_digest
    assert first.baseline_observation_digest == second.baseline_observation_digest
    third = await comparison(factory, repo)
    assert third.status == "equal"
    assert third.candidate_input_digest != first.candidate_input_digest


async def test_lag_gate_never_prepares_replays_or_locks(candidate_db, monkeypatch):
    factory, repo, policy, seq = candidate_db
    await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        head = await session.get(
            ProjectionHeadRow, (repo.account_id, repo.environment, "execution_state")
        )
        assert head is not None
        head.last_event_seq -= 1

    def forbidden(*args, **kwargs):
        raise AssertionError("baseline attempted a writer/lock path")

    for cls, name in (
        (CapitalRepository, "_prepare"),
        (AccountEventWriter, "prepare_locked"),
        (AccountEventWriter, "_replay_pending"),
        (AccountEventWriter, "acquire_lock"),
    ):
        monkeypatch.setattr(cls, name, forbidden)
    result = await comparison(factory, repo)
    assert result.status == "not_comparable"
    assert result.reason == "projection_cursor_lag"
    assert result.classifications == ("projection_integrity",)
    assert result.heads.projection_cursor < result.heads.watermark


@pytest.mark.parametrize("fault", ["attempt", "uncertainty"])
async def test_projection_corruption_keeps_candidate_evidence(candidate_db, fault):
    factory, repo, policy, seq = candidate_db
    await authorize(factory, repo, policy, seq)
    before = await comparison(factory, repo)
    assert before.status == "equal", before
    async with factory.begin() as session:
        if fault == "attempt":
            await session.execute(update(SubmissionAttemptRow).values(outcome_kind="not_sent"))
        else:
            session.add(ExecutionUncertaintyRow(
                exchange_account_id=repo.account_id,
                deployment_environment=repo.environment,
                symbol="fUST",
                kind="unsupported_venue_exposure",
                correlation_key="corrupt-shadow-row",
                intended_amount=Decimal("0"),
                evidence={},
                opened_event_seq=seq,
            ))
    after = await comparison(factory, repo)
    assert after.candidate_input_digest == before.candidate_input_digest
    assert after.status == "different" or (
        after.status == "not_comparable" and after.reason == "projection_integrity"
    ), after


async def test_decoder_failure_is_error(candidate_db, monkeypatch):
    factory, repo, *_ = candidate_db
    from bfx_funding_bot.modules.trading_shadow._internal import loader as loader_module

    async def broken(*args, **kwargs):
        raise RuntimeError("sensitive details")

    monkeypatch.setattr(loader_module.CandidateLoader, "_load", broken)
    result = await comparison(factory, repo)
    assert result.status == "error" and result.reason == "RuntimeError"
    assert "sensitive details" not in repr(result)
