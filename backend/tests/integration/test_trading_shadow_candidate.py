"""Isolated PostgreSQL candidate tests. No baseline adapter or runtime wiring."""

import json
import re
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy import event as sa_event

from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyRevisionRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, EventPrefixHashRow
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationUnknown,
    UncertaintyBoundToVenueOffer,
    UncertaintyManuallyResolved,
    UncertaintyMarkedNotAccepted,
    VenueOfferQuarantined,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.trading import Available, Blocked, CapitalScope, derive_capital
from bfx_funding_bot.modules.trading_shadow import LoadedInputs, NotComparable, ScanLimits
from bfx_funding_bot.modules.trading_shadow._internal import loader as loader_module
from bfx_funding_bot.modules.trading_shadow.wiring import build_candidate_loader
from tests.integration.test_capital_repository import (
    authorize,
    intent,
    repository,
    seed_historical_cycles,
    setup_policy,
    snapshot,
)
from tests.modules.execution.event_store.test_uncertainty_resolution_events import (
    ACCOUNT,
    ENV,
    _open_unknown,
    _snapshot,
)

pytestmark = pytest.mark.integration
ALLOWLIST = {
    "capital_snapshots",
    "capital_snapshot_queries",
    "capital_policy_heads",
    "capital_policy_revisions",
    "execution_decisions",
    "event_log",
    "event_prefix_hashes",
}
DEFAULT_SCAN_LIMITS = ScanLimits()


def relations(sql):
    """Test-only relation scanner: aliases, comma joins, CTEs and nested SELECTs.

    Tokenize strings/comments away, walk every depth, and resolve CTE names
    separately. Function calls are not relations. Fixtures exercise the scanner
    against projection reads hidden behind aliases, JOIN, CTE and subquery.
    """
    cleaned = re.sub(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'", " ", sql, flags=re.S)
    tokens = re.findall(r'"(?:""|[^"])*"|[A-Za-z_][A-Za-z_0-9]*|[(),.]', cleaned)
    tokens = [token.strip('"').lower() for token in tokens]
    ctes = {tokens[i] for i in range(len(tokens) - 2) if tokens[i + 1 : i + 3] == ["as", "("]}
    result = set()
    # Each nesting depth has its own FROM-list state.
    from_lists = [False]
    expect_relation = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "(":
            from_lists.append(False)
            expect_relation = False
        elif token == ")":
            from_lists.pop()
            expect_relation = False
        elif token in {"from", "join"}:
            from_lists[-1] = True
            expect_relation = True
        elif token in {"where", "group", "order", "limit", "union", "on", "having"}:
            from_lists[-1] = False
            expect_relation = False
        elif token == "," and from_lists[-1]:
            expect_relation = True
        elif expect_relation:
            if i + 2 < len(tokens) and tokens[i + 1] == ".":
                token = tokens[i + 2]
                i += 2
            if token not in ctes:
                result.add(token)
            expect_relation = False
        i += 1
    return result


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s.id FROM submission_attempts AS s",
        "SELECT e.event_seq FROM event_log e JOIN submission_attempts s ON true",
        "WITH hidden AS (SELECT id FROM submission_attempts) SELECT id FROM hidden",
        "SELECT x.id FROM (SELECT id FROM submission_attempts) AS x",
        "SELECT e.event_seq FROM event_log e, public.submission_attempts s",
    ],
)
def test_sql_capture_scanner_sees_nested_projection_reads(sql):
    assert relations(sql) - ALLOWLIST == {"submission_attempts"}


@pytest_asyncio.fixture
async def candidate_db(pg_session_factory):
    factory = pg_session_factory
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="shadow-candidate"))
    repo = repository(account)
    policy = await setup_policy(factory, repo)
    seq = await snapshot(factory, repo)
    return factory, repo, policy, seq


async def begin_read(session):
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


async def load(factory, repo, *, limits=DEFAULT_SCAN_LIMITS, session=None, cell="a30"):
    loader = build_candidate_loader(source_revision="test-source", limits=limits)
    kwargs = {
        "scope": CapitalScope(repo.account_id, repo.environment, "fUST", cell),
        "now_ms": 2000,
        "max_snapshot_age_ms": 10000,
    }
    if session is not None:
        return await loader.load(session, **kwargs)
    async with factory.begin() as reader:
        await begin_read(reader)
        return await loader.load(reader, **kwargs)


def fold(loaded):
    assert isinstance(loaded, LoadedInputs), loaded
    i = loaded.inputs
    return derive_capital(
        scope=i.scope,
        policy=i.policy,
        accepted=i.accepted,
        attempts=i.attempts,
        uncertainties=i.uncertainties,
        read_context=i.read_context,
    )


async def outcome(factory, repo, opening, kind):
    kwargs = {
        "symbol": opening.symbol,
        "cid": opening.cid,
        "signal_correlation_id": opening.signal_correlation_id,
        "account_id": str(repo.account_id),
        "is_simulated": True,
        "amount": opening.amount,
        "occurred_at_ms": 1150,
    }
    if kind == "acknowledged":
        event = ReservationClaimed(
            **kwargs,
            venue_offer_id="offer-1",
            reservation_ref=replace(opening.reservation_ref, venue_offer_id="offer-1"),
        )
    else:
        cls = ReservationUnknown if kind == "unknown" else ReservationFailed
        event = cls(
            **kwargs,
            reservation_ref=opening.reservation_ref,
            reason="local_pre_transport" if kind == "not_sent" else "test",
        )
    async with factory.begin() as session:
        await repo.writer.append(session, event)


@pytest.mark.parametrize("kind", ["pending", "acknowledged", "not_sent", "rejected", "unknown"])
async def test_facts_map_real_writer_events(candidate_db, kind):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    if kind != "pending":
        await outcome(factory, repo, admitted.intent, kind)
    loaded = await load(factory, repo)
    assert isinstance(loaded, LoadedInputs), loaded
    assert loaded.inputs.attempts[0].outcome == kind
    view = fold(loaded)
    if kind == "unknown":
        assert isinstance(view, Blocked) and view.reason == "execution_unknown"
    else:
        assert isinstance(view, Available)
        assert view.view.snapshot.unreflected_commitments == (
            200 if kind in {"pending", "acknowledged"} else 0
        )


async def test_sql_allowlist_and_projection_corruption_do_not_change_candidate(
    candidate_db, pg_engine
):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    await outcome(factory, repo, admitted.intent, "unknown")
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    sa_event.listen(pg_engine.sync_engine, "before_cursor_execute", capture)
    try:
        before = await load(factory, repo)
    finally:
        sa_event.remove(pg_engine.sync_engine, "before_cursor_execute", capture)
    assert isinstance(before, LoadedInputs), before
    seen = set().union(*(relations(sql) for sql in captured))
    assert seen == ALLOWLIST
    assert all(
        "for update" not in sql.lower() and "select *" not in sql.lower() for sql in captured
    )
    async with factory.begin() as session:
        await session.execute(delete(ExecutionUncertaintyRow))
        await session.execute(update(SubmissionAttemptRow).values(outcome_kind="not_sent"))
    assert await load(factory, repo) == before
    async with factory.begin() as session:
        await session.execute(delete(SubmissionAttemptRow))
    assert await load(factory, repo) == before


async def test_select_only_authority_role_can_load(candidate_db, pg_engine):
    factory, repo, policy, seq = candidate_db
    await authorize(factory, repo, policy, seq)
    role = "shadow_" + uuid4().hex
    # This role is local to the isolated testcontainer database; no host auth changes.
    async with pg_engine.begin() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await conn.execute(text(f'GRANT SELECT ON {", ".join(sorted(ALLOWLIST))} TO "{role}"'))
    try:
        async with factory.begin() as session:
            await begin_read(session)
            await session.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert not await session.scalar(
                text("SELECT has_table_privilege(current_user, 'submission_attempts', 'SELECT')")
            )
            assert not await session.scalar(
                text("SELECT has_table_privilege(current_user, 'event_log', 'INSERT')")
            )
            assert isinstance(await load(factory, repo, session=session), LoadedInputs)
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))


async def test_mvcc_hides_concurrent_outcome_query_and_policy(candidate_db):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    async with factory.begin() as reader:
        await begin_read(reader)
        before = await load(factory, repo, session=reader)
        assert isinstance(before, LoadedInputs), before
        await outcome(factory, repo, admitted.intent, "rejected")
        async with factory.begin() as writer:
            await repo.begin_snapshot(writer, now_ms=1200)
            await repo.apply_policy(
                writer,
                symbol="fUST",
                policy=replace(policy.policy, reserve_amount=Decimal("120")),
                expected_revision=policy.revision,
                source={"operator": "test"},
            )
        assert await load(factory, repo, session=reader) == before
    after = await load(factory, repo)
    assert isinstance(after, LoadedInputs), after
    assert after.inputs.attempts[0].outcome == "rejected"
    assert after.inputs.policy.revision == policy.revision + 1
    assert after.inputs.read_context.latest_query_id != before.inputs.read_context.latest_query_id
    assert after.candidate_input_digest != before.candidate_input_digest


@pytest.mark.parametrize("fault", ["policy", "classification", "prefix", "decision"])
async def test_missing_or_corrupt_evidence_is_not_comparable(candidate_db, fault):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    async with factory.begin() as session:
        if fault == "policy":
            await session.execute(update(CapitalPolicyRevisionRow).values(digest="bad"))
        elif fault == "classification":
            row = await session.get(CapitalSnapshotRow, seq)
            changed = json.loads(json.dumps(row.classification))
            changed["symbols"]["fUST"]["available"] = "9999"
            row.classification = changed
        elif fault == "prefix":
            await session.execute(
                update(EventPrefixHashRow)
                .where(EventPrefixHashRow.event_seq == seq)
                .values(prefix_hash="bad")
            )
        else:
            # create_all has no authority immutability triggers; retain the FK ID,
            # move the decision out of scope so the candidate must observe it missing.
            await session.execute(
                update(ExecutionDecisionRow)
                .where(ExecutionDecisionRow.decision_id == admitted.intent.execution_decision_id)
                .values(deployment_environment="other")
            )
    loaded = await load(factory, repo)
    assert isinstance(loaded, NotComparable), loaded
    assert loaded.classification == "input_evidence_gap"
    assert (
        loaded.reason
        == {
            "policy": "invalid_policy_schema_or_digest",
            "classification": "snapshot_evidence_conflict",
            "prefix": "snapshot_prefix_diverged",
            "decision": "attempt_decision_conflict",
        }[fault]
    )


@pytest.mark.parametrize("limit", ["rows", "bytes", "references"])
async def test_caps_refuse_instead_of_truncating(candidate_db, limit):
    factory, repo, policy, seq = candidate_db
    first = await authorize(factory, repo, policy, seq)
    await outcome(factory, repo, first.intent, "rejected")
    await authorize(factory, repo, policy, seq, cid=2)
    limits = {
        "rows": ScanLimits(max_history_rows=1),
        "bytes": ScanLimits(max_payload_bytes=1),
        "references": ScanLimits(max_reference_lookups=1),
    }[limit]
    loaded = await load(factory, repo, limits=limits)
    assert isinstance(loaded, NotComparable) and loaded.classification == "input_evidence_gap"
    assert (
        loaded.reason
        == {
            "rows": "history_row_limit",
            "bytes": "history_payload_limit",
            "references": "reference_lookup_limit",
        }[limit]
    )


async def test_uncertainty_set_over_cap_is_not_comparable(candidate_db):
    factory, repo, _, _ = candidate_db
    # The legacy store only quarantines offers a prior snapshot observed.
    await snapshot(
        factory,
        repo,
        offers=tuple(
            VenueOfferObservation(
                key, "fUST", Decimal("10"), Decimal("10"), Decimal("0.001"), 2, "active", 1000, 1100
            )
            for key in ("foreign-1", "foreign-2")
        ),
    )
    async with factory.begin() as session:
        for offer_id in ("foreign-1", "foreign-2"):
            await repo.writer.append(
                session,
                VenueOfferQuarantined(
                    offer_id,
                    "fUST",
                    amount=Decimal("10"),
                    account_id=str(repo.account_id),
                    occurred_at_ms=1200,
                ),
            )
    loaded = await load(factory, repo, limits=ScanLimits(max_history_rows=1))
    assert isinstance(loaded, NotComparable) and loaded.reason == "history_row_limit"


async def test_snapshot_history_over_cap_uses_only_accepted_payload(candidate_db, pg_engine):
    factory, repo, _, accepted_seq = candidate_db
    async with factory.begin() as session:
        for finished in (1201, 1202, 1203):
            await repo.writer.append(
                session,
                replace(
                    _snapshot(finished=finished), account_id=str(repo.account_id), event_id=uuid4()
                ),
            )
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))

    sa_event.listen(pg_engine.sync_engine, "before_cursor_execute", capture)
    try:
        loaded = await load(factory, repo, limits=ScanLimits(max_history_rows=2))
    finally:
        sa_event.remove(pg_engine.sync_engine, "before_cursor_execute", capture)
    assert isinstance(loaded, LoadedInputs), loaded
    folded = fold(loaded)
    assert isinstance(folded, Blocked)
    assert folded.reason == "snapshot_superseded_by_unfenced_observation"
    assert loaded.inputs.accepted.snapshot_seq == accepted_seq
    assert loaded.inputs.read_context.latest_observation_seq > accepted_seq
    work = json.loads(loaded.manifest.work_json)
    assert work["uncertainty_rows"] == work["tail_rows"] == 0
    assert work["point_lookups"] == 1
    assert work["seq_only_rows"] == 1
    evidence = json.loads(loaded.manifest.evidence_json)
    assert [row["event_seq"] for row in evidence["events"]] == [accepted_seq]
    payload_queries = [
        (sql, params)
        for sql, params in captured
        if "event_log" in sql
        and "payload" in sql.lower()
        and not sql.startswith("SELECT event_seq, octet_length")
    ]
    assert len(payload_queries) == 3  # two empty typed sets and the accepted point lookup
    assert all(
        "VENUE_SNAPSHOT_OBSERVED" not in str(params)
        for sql, params in payload_queries
        if "event_type IN" in sql
    )
    assert (
        sum("event_seq = " in sql and "event_type IN" not in sql for sql, _ in payload_queries) == 1
    )


async def test_partial_fill_uses_remaining_offer_and_credit_once(candidate_db):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    await outcome(factory, repo, admitted.intent, "acknowledged")
    offer = VenueOfferObservation(
        "offer-1", "fUST", Decimal("200"), Decimal("50"), Decimal("0.0001"), 2, "active", 1000, 1100
    )
    credit = VenueCreditObservation("c1", "fUST", Decimal("150"), Decimal("0.0001"), 2, "active")
    await snapshot(factory, repo, "800", offers=(offer,), credits=(credit,))
    loaded = await load(factory, repo)
    view = fold(loaded)
    assert isinstance(view, Available)
    assert view.view.snapshot.total_capital == 1000
    assert view.view.snapshot.cell_exposure == 200
    assert view.view.snapshot.unreflected_commitments == 0
    assert view.view.unattributed_credit_exposure == 0


async def test_tail_outcomes_and_legacy_intent_gap(candidate_db):
    # CID reuse needs a proven-terminal prior cycle, which only historical rows
    # provide; see test_accepted_legacy_cycles_keep_authorization_proof.
    factory, repo, policy, seq = candidate_db
    first = await authorize(factory, repo, policy, seq, cid=7)
    await outcome(factory, repo, first.intent, "not_sent")
    await authorize(factory, repo, policy, seq, cid=9)
    loaded = await load(factory, repo)
    assert isinstance(loaded, LoadedInputs), loaded
    assert [a.outcome for a in loaded.inputs.attempts] == ["not_sent", "pending"]
    legacy, decision = intent(repo.account_id, cid=8)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        await repo.writer.append(session, replace(legacy, submission_attempt=None))
    gap = await load(factory, repo)
    assert isinstance(gap, NotComparable) and gap.reason == "attempt_intent_missing"


async def test_legacy_unknown_without_attempt_still_blocks(candidate_db):
    factory, repo, _, _ = candidate_db
    legacy, decision = intent(repo.account_id)
    async with factory.begin() as session:
        session.add(decision)
        await session.flush()
        # No intent/attempt, as supported by historical writer consumers.
        await repo.writer.append(
            session,
            ReservationUnknown(
                symbol="fUST",
                cid=legacy.cid,
                signal_correlation_id=legacy.signal_correlation_id,
                account_id=str(repo.account_id),
                is_simulated=True,
                reservation_ref=legacy.reservation_ref,
                amount=legacy.amount,
                reason="legacy_timeout",
                occurred_at_ms=1200,
            ),
        )
    loaded = await load(factory, repo)
    assert isinstance(loaded, LoadedInputs), loaded
    assert loaded.inputs.attempts == ()
    assert len(loaded.inputs.uncertainties) == 1 and loaded.inputs.uncertainties[0].is_open
    assert isinstance(fold(loaded), Blocked)


@pytest.mark.parametrize("kind", ["not_accepted", "bound"])
async def test_unknown_resolution_and_stale_acceptance(pg_session_factory, kind):
    factory = pg_session_factory
    identity = await _open_unknown(factory)
    repo = repository(ACCOUNT, ENV)
    await setup_policy(factory, repo)
    # Acceptance while UNKNOWN is open records unresolved, even after later resolution.
    await snapshot(factory, repo)
    async with factory.begin() as session:
        observed = await repo.writer.append(
            session, _snapshot(finished=1201, offer=kind == "bound")
        )
        evidence = {
            "reconcile_event_seq": observed.event_seq,
            "query_started_at_ms": 1200,
            "query_finished_at_ms": 1201,
            "candidate_count": 0 if kind == "not_accepted" else 1,
        }
        kwargs = {
            "uncertainty_id": identity,
            "account_id": str(ACCOUNT),
            "environment": ENV,
            "symbol": "fUST",
            "kind": "submit_outcome_unknown",
            "reconcile_event_seq": observed.event_seq,
            "resolved_by_operator_id": "test",
            "resolution_reason": "proven",
            "resolution_evidence": evidence,
            "occurred_at_ms": 1202,
        }
        if kind == "not_accepted":
            resolution = UncertaintyMarkedNotAccepted(**kwargs, candidate_count=0)
        else:
            evidence["venue_offer_id"] = "venue-1"
            resolution = UncertaintyBoundToVenueOffer(
                **kwargs, venue_offer_id="venue-1", venue_status="active"
            )
        await repo.writer.append(session, resolution)
    loaded = await load(factory, repo, cell="cell-1")
    assert isinstance(loaded, LoadedInputs), loaded
    assert loaded.inputs.accepted.unresolved_attempts
    assert not loaded.inputs.uncertainties[0].is_open
    assert isinstance(fold(loaded), Blocked)  # Resolution cannot refresh old acceptance.


async def test_quarantine_merge_and_manual_resolution(candidate_db):
    factory, repo, _, _ = candidate_db
    offers = tuple(
        VenueOfferObservation(
            key,
            "fUST",
            Decimal("10"),
            Decimal("10"),
            Decimal("0.001"),
            2,
            "active",
            1000,
            1100,
        )
        for key in ("foreign-1", "foreign-2")
    )
    await snapshot(factory, repo, offers=offers)
    first = VenueOfferQuarantined(
        "foreign-1",
        "fUST",
        amount=Decimal("10"),
        account_id=str(repo.account_id),
        occurred_at_ms=1100,
    )
    async with factory.begin() as session:
        await repo.writer.append(session, first)
        await repo.writer.append(
            session, replace(first, event_id=uuid4(), venue_offer_id="foreign-2")
        )
    loaded = await load(factory, repo)
    assert isinstance(loaded, LoadedInputs), loaded
    identity = uuid5(UUID("b1f89542-a63e-584a-b5bc-cd448ed74f3f"), str(first.event_id))
    assert len(loaded.inputs.uncertainties) == 1
    assert loaded.inputs.uncertainties[0].uncertainty_id == identity
    async with factory.begin() as session:
        observed = await repo.writer.append(
            session, replace(_snapshot(finished=1201), account_id=str(repo.account_id))
        )
        await repo.writer.append(
            session,
            UncertaintyManuallyResolved(
                uncertainty_id=identity,
                account_id=str(repo.account_id),
                environment=ENV,
                symbol="fUST",
                kind="unattributed_venue_offer",
                reconcile_event_seq=observed.event_seq,
                resolved_by_operator_id="test",
                resolution_reason="verified",
                resolution_action="closed_at_venue",
                resolution_evidence={
                    "reconcile_event_seq": observed.event_seq,
                    "query_started_at_ms": 1200,
                    "query_finished_at_ms": 1201,
                },
                occurred_at_ms=1202,
            ),
        )
    after = await load(factory, repo)
    assert isinstance(after, LoadedInputs), after
    assert not after.inputs.uncertainties[0].is_open


async def test_other_scopes_do_not_affect_heads_or_digest(candidate_db):
    factory, repo, _, _ = candidate_db
    before = await load(factory, repo)
    other = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=other, venue="bitfinex", label="other"))
    other_repo = repository(other)
    await setup_policy(factory, other_repo)
    await snapshot(factory, other_repo)
    assert await load(factory, repo) == before


async def test_rejects_read_committed_or_writable_session(candidate_db):
    factory, repo, _, _ = candidate_db
    async with factory.begin() as session:
        loaded = await load(factory, repo, session=session)
    assert isinstance(loaded, NotComparable)
    assert loaded.classification == "input_evidence_gap"


async def test_digest_is_row_order_stable_and_covers_consumed_evidence(candidate_db, monkeypatch):
    factory, repo, policy, seq = candidate_db
    admitted = await authorize(factory, repo, policy, seq)
    await outcome(factory, repo, admitted.intent, "rejected")
    before = await load(factory, repo)
    assert isinstance(before, LoadedInputs), before
    original_rows = loader_module._rows

    async def reversed_rows(session, sql, params):
        return list(reversed(await original_rows(session, sql, params)))

    monkeypatch.setattr(loader_module, "_rows", reversed_rows)
    assert await load(factory, repo) == before
    async with factory.begin() as session:
        row = await session.scalar(
            select(EventLogRow).where(EventLogRow.event_type == "RESERVATION_FAILED")
        )
        # create_all fixture intentionally has no append-only trigger. Alter a
        # consumed diagnostic, leaving normalized capital facts unchanged.
        row.payload = {**row.payload, "reason": "different_rejection_diagnostic"}
    changed = await load(factory, repo)
    assert isinstance(changed, LoadedInputs), changed
    assert changed.inputs == before.inputs
    assert changed.candidate_input_digest != before.candidate_input_digest


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("terminal", ["ORDER_FILL", "RESERVATION_RELEASED"])
async def test_accepted_legacy_cycles_keep_authorization_proof(
    pg_session_factory, partial, terminal
):
    factory = pg_session_factory
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="legacy-shadow"))
    sequences = await seed_historical_cycles(
        factory, account, reused=not partial, terminal=terminal
    )
    if partial:
        async with factory.begin() as session:
            row = await session.get(EventLogRow, sequences[-1])
            row.payload = {**row.payload, "amount": "2", "size_usdt": "2"}
    repo = repository(account)
    await setup_policy(factory, repo)
    await snapshot(factory, repo)
    loaded = await load(factory, repo)
    assert isinstance(loaded, LoadedInputs), loaded
    assert loaded.inputs.attempts == ()
    result = fold(loaded)
    if partial:
        assert isinstance(result, Blocked) and result.reason == "unclassifiable_legacy_intent"
    else:
        assert isinstance(result, Available)
        assert result.view.snapshot.unreflected_commitments == 0
        assert result.view.budget.spendable == 900
