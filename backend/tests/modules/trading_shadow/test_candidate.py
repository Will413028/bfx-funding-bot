"""Pure compatibility and completeness tests; legacy code is a test oracle only."""

import json
from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal, localcontext
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4, uuid5

import pytest

from bfx_funding_bot.modules.execution.event_store.projector import derive_v2_event_id
from bfx_funding_bot.modules.execution.event_store.serialization import (
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationUnknown,
    SnapshotCoverage,
    SubmitMatchedToVenueOffer,
    UncertaintyBoundToVenueOffer,
    UncertaintyManuallyResolved,
    UncertaintyMarkedNotAccepted,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.trading import (
    Available,
    CapitalPolicy,
    CapitalScope,
    derive_capital,
    policy_payload,
)
from bfx_funding_bot.modules.trading_shadow import (
    LoadedInputs,
    NotComparable,
    ScanLimits,
    candidate_input_digest,
    canonical_bytes,
)
from bfx_funding_bot.modules.trading_shadow._internal import loader as loader_module
from bfx_funding_bot.modules.trading_shadow._internal.evidence import (
    EvidenceError,
    decode_basis,
    decode_event,
    decode_policy,
    legacy_digest,
)
from bfx_funding_bot.modules.trading_shadow._internal.facts import decode_facts
from bfx_funding_bot.modules.trading_shadow.wiring import build_candidate_loader
from tests.integration.test_capital_repository import intent
from tests.modules.execution.event_store.test_uncertainty_resolution_events import (
    ACCOUNT,
    ENV,
    _attempt,
    _decision,
    _snapshot,
)

SCOPE = CapitalScope(ACCOUNT, ENV, "fUST", "cell-1")


def event_row(event, seq):
    payload = serialize_event(event)
    return {
        "event_seq": seq,
        "account_id": str(ACCOUNT),
        "exchange_account_id": ACCOUNT,
        "deployment_environment": ENV,
        "event_type": event_type_of(event),
        "cid": getattr(event, "cid", None),
        "venue_offer_id": getattr(event, "venue_offer_id", None),
        "venue_seq": None,
        "event_id": event.event_id,
        "schema_version": 3,
        "payload": payload,
        "payload_bytes": len(canonical_bytes(payload)),
        "occurred_at_ms": event.occurred_at_ms or 0,
    }


def decoded(event, seq):
    return decode_event(event_row(event, seq), SCOPE, 1000)


def decision_row(row):
    return {
        key: getattr(row, key)
        for key in (
            "decision_id",
            "exchange_account_id",
            "deployment_environment",
            "cell_id",
            "symbol",
            "signal_correlation_id",
            "amount_usdt",
        )
    }


def base_rows():
    revision_id, query_id = uuid4(), uuid4()
    raw_policy = policy_payload(CapitalPolicy(True, Decimal("100"), max_cell_fraction=Decimal("1")))
    head = {
        "exchange_account_id": ACCOUNT,
        "deployment_environment": ENV,
        "symbol": "fUST",
        "revision_id": revision_id,
        "revision": 1,
    }
    policy = {
        "id": revision_id,
        "exchange_account_id": ACCOUNT,
        "deployment_environment": ENV,
        "symbol": "fUST",
        "revision": 1,
        "schema_version": 1,
        "policy": raw_policy,
        "digest": legacy_digest(raw_policy),
    }
    classification = {
        "symbols": {
            "fUST": {
                "available": "800",
                "offered": "50",
                "credits": "150",
                "unattributed_credits": "150",
                "foreign": "40",
                "cells": {"cell-1": "50"},
            }
        },
        "reflected": {},
        "settled": [],
        "unresolved": {},
        "credit_cells": {},
    }
    query = {
        "id": query_id,
        "exchange_account_id": ACCOUNT,
        "deployment_environment": ENV,
        "command_fence": 0,
        "query_revision": 1,
        "started_at_ms": 1000,
    }
    observation = VenueSnapshotObserved(
        account_id=str(ACCOUNT),
        environment=ENV,
        query_started_at_ms=1000,
        query_finished_at_ms=1050,
        offers=(),
        credits=(),
        wallet_available={"fUST": Decimal("800")},
        coverage=SnapshotCoverage(True, True, True),
    )
    observation = replace(
        observation,
        capital_query_id=str(query_id),
        capital_command_fence=0,
        capital_classification_digest=legacy_digest(classification),
        capital_confirmation=serialize_event(replace(observation, event_id=uuid4())),
    )
    accepted = {
        "event_seq": 1,
        "query_id": query_id,
        "exchange_account_id": ACCOUNT,
        "deployment_environment": ENV,
        "schema_version": 1,
        "command_fence": 0,
        "classification": classification,
        "covered_prefix_hash": "prefix",
        "authorization_blocked_reason": None,
    }
    return head, policy, accepted, query, event_row(observation, 1)


def test_canonical_precision_order_and_missing_vs_null():
    with localcontext() as context:
        context.prec = 2
        assert canonical_bytes(Decimal("123456.78000")) == b'"123456.78"'
        assert canonical_bytes(Decimal("-0.000")) == b'"0"'
    assert canonical_bytes(
        {"b": {UUID(int=2), UUID(int=1)}, "a": Decimal("1E+4")}
    ) == canonical_bytes({"a": Decimal("10000"), "b": {UUID(int=1), UUID(int=2)}})
    assert candidate_input_digest({}) != candidate_input_digest({"x": None})
    with pytest.raises(ValueError):
        canonical_bytes(float("nan"))


@pytest.mark.parametrize("fault", ["digest", "keys", "scope", "schema", "amount"])
def test_policy_proof_cannot_be_skipped(fault):
    head, policy, *_ = base_rows()
    assert decode_policy(SCOPE, head, policy).policy.enabled
    if fault == "digest":
        policy["digest"] = "bad"
    elif fault == "keys":
        policy["policy"]["extra"] = None
        policy["digest"] = legacy_digest(policy["policy"])
    elif fault == "scope":
        policy["exchange_account_id"] = uuid4()
    elif fault == "schema":
        policy["schema_version"] = 999
    else:
        policy["policy"]["reserve_amount"] = "NaN"
        policy["digest"] = legacy_digest(policy["policy"])
    with pytest.raises(EvidenceError):
        decode_policy(SCOPE, head, policy)


@pytest.mark.parametrize("fault", ["classification", "prefix", "query", "overlap"])
def test_basis_proof_cannot_be_skipped(fault):
    _, _, accepted, query, row = base_rows()
    event = decode_event(row, SCOPE, 1)
    prefix = "prefix"
    if fault == "classification":
        accepted["classification"]["symbols"]["fUST"]["available"] = "999"
    elif fault == "prefix":
        prefix = "bad"
    elif fault == "query":
        query["started_at_ms"] = 999
    else:
        identity = str(uuid4())
        accepted["classification"]["settled"] = [identity, identity]
        row["payload"]["capital_classification_digest"] = legacy_digest(accepted["classification"])
        event = decode_event(row, SCOPE, 1)
    with pytest.raises(EvidenceError):
        decode_basis(SCOPE, accepted, query, event, prefix, 1)


@pytest.mark.parametrize("fault", ["missing_id", "inverted_interval"])
def test_confirmation_requires_valid_serialized_snapshot(fault):
    _, _, accepted, query, row = base_rows()
    confirmation = row["payload"]["capital_confirmation"]
    if fault == "missing_id":
        del confirmation["event_id"]
    else:
        confirmation["query_finished_at_ms"] = confirmation["query_started_at_ms"] - 1
    with pytest.raises(EvidenceError, match="snapshot_confirmation_conflict"):
        decode_basis(SCOPE, accepted, query, decode_event(row, SCOPE, 1), "prefix", 1)


@pytest.mark.parametrize("fault", ["scope", "realm", "watermark", "schema", "identity"])
def test_event_scope_and_watermark_are_proven(fault):
    *_, row = base_rows()
    watermark = 1
    if fault == "scope":
        row["exchange_account_id"] = uuid4()
    elif fault == "realm":
        row["deployment_environment"] = "live"
    elif fault == "watermark":
        watermark = 0
    elif fault == "schema":
        row["schema_version"] = 2
    else:
        row["event_id"] = uuid4()
    with pytest.raises(EvidenceError):
        decode_event(row, SCOPE, watermark)


@pytest.mark.parametrize("outcome", ["pending", "acknowledged", "not_sent", "rejected", "unknown"])
def test_attempt_mapping_and_decision_identity(outcome):
    opening, decision = intent(ACCOUNT)
    history = [decoded(opening, 2)]
    if outcome != "pending":
        kwargs = {
            "symbol": "fUST",
            "cid": opening.cid,
            "signal_correlation_id": opening.signal_correlation_id,
            "account_id": str(ACCOUNT),
            "is_simulated": True,
            "amount": opening.amount,
            "occurred_at_ms": 1150,
        }
        if outcome == "acknowledged":
            event = ReservationClaimed(
                **kwargs,
                venue_offer_id="offer",
                reservation_ref=replace(opening.reservation_ref, venue_offer_id="offer"),
            )
        else:
            cls = ReservationUnknown if outcome == "unknown" else ReservationFailed
            event = cls(
                **kwargs,
                reservation_ref=opening.reservation_ref,
                reason="local_pre_transport" if outcome == "not_sent" else "failed",
            )
        history.append(decoded(event, 3))
    decisions = {decision.decision_id: decision_row(decision)}
    attempts, uncertainties, _ = decode_facts(SCOPE, tuple(history), decisions, 1, 10)
    assert attempts[0].outcome == outcome
    assert attempts[0].intent_seq == 2
    assert bool(uncertainties) == (outcome == "unknown")
    with pytest.raises(EvidenceError, match="attempt_decision_conflict"):
        decode_facts(SCOPE, tuple(history), {}, 1, 10)


def test_reused_cid_never_links_the_wrong_decision():
    first, d1 = intent(ACCOUNT, cid=7)
    second, d2 = intent(ACCOUNT, cid=7)
    failure = ReservationFailed(
        symbol="fUST",
        cid=7,
        signal_correlation_id=first.signal_correlation_id,
        account_id=str(ACCOUNT),
        is_simulated=True,
        amount=first.amount,
        reservation_ref=first.reservation_ref,
        reason="local_pre_transport",
        occurred_at_ms=1200,
    )
    attempts, _, _ = decode_facts(
        SCOPE,
        (decoded(first, 2), decoded(failure, 3), decoded(second, 4)),
        {d1.decision_id: decision_row(d1), d2.decision_id: decision_row(d2)},
        1,
        10,
    )
    assert [(a.intent_seq, a.outcome) for a in attempts] == [(2, "not_sent"), (4, "pending")]


def test_post_fence_legacy_intent_is_not_zero_commitment():
    opening, _ = intent(ACCOUNT)
    event = decoded(replace(opening, submission_attempt=None), 2)
    with pytest.raises(EvidenceError, match="attempt_intent_missing"):
        decode_facts(SCOPE, (event,), {}, 1, 10)
    assert decode_facts(SCOPE, (event,), {}, 2, 10)[0] == ()


def test_legacy_unknown_identity_uses_original_account_and_blocks_without_attempt():
    opening, _ = intent(ACCOUNT)
    unknown = ReservationUnknown(
        symbol="fUST",
        cid=opening.cid,
        signal_correlation_id=opening.signal_correlation_id,
        account_id=str(ACCOUNT),
        is_simulated=True,
        reservation_ref=opening.reservation_ref,
        amount=opening.amount,
        reason="legacy",
        occurred_at_ms=1200,
    )
    row = event_row(unknown, 3)
    row.update(account_id="legacy-account", event_id=None, schema_version=2)
    row["payload"].pop("__schema_version__")
    row["payload"].pop("event_id")
    row["payload"].pop("symbol")
    expected = derive_v2_event_id(
        event_seq=3,
        account_id="legacy-account",
        deployment_environment=ENV,
        event_type=row["event_type"],
        occurred_at_ms=1200,
        payload=row["payload"],
    )
    event = decode_event(row, SCOPE, 3)
    assert event.event_id == expected
    _, uncertainties, _ = decode_facts(SCOPE, (event,), {}, 1, 10)
    assert uncertainties[0].uncertainty_id == uuid5(
        UUID("d158ef54-c1dd-54e4-a9e9-9a670c938f73"), str(expected)
    )
    assert uncertainties[0].is_open and uncertainties[0].symbol == "fUST"


@pytest.mark.parametrize("kind", ["match", "bound", "not_accepted"])
def test_resolution_keeps_unknown_outcome_and_requires_reconcile_evidence(kind):
    from bfx_funding_bot.modules.execution.events import ReservationIntent

    payload = _attempt()
    decision = _decision()
    opening = ReservationIntent(
        symbol="fUST",
        cid=payload.cid,
        signal_correlation_id=UUID(decision.signal_correlation_id),
        account_id=str(ACCOUNT),
        is_simulated=True,
        execution_decision_id=payload.execution_decision_id,
        submission_attempt=payload,
        amount=Decimal("100"),
        occurred_at_ms=2,
    )
    unknown = ReservationUnknown(
        symbol="fUST",
        cid=payload.cid,
        signal_correlation_id=opening.signal_correlation_id,
        account_id=str(ACCOUNT),
        is_simulated=True,
        reservation_ref=opening.reservation_ref,
        amount=Decimal("100"),
        reason="timeout",
        occurred_at_ms=3,
    )
    identity = uuid5(UUID("d158ef54-c1dd-54e4-a9e9-9a670c938f73"), str(unknown.event_id))
    observation = _snapshot(finished=5, offer=kind != "not_accepted")
    evidence = {
        "reconcile_event_seq": 4,
        "query_started_at_ms": 4,
        "query_finished_at_ms": 5,
        "candidate_count": 0 if kind == "not_accepted" else 1,
    }
    common = {
        "uncertainty_id": identity,
        "account_id": str(ACCOUNT),
        "environment": ENV,
        "symbol": "fUST",
        "kind": "submit_outcome_unknown",
        "reconcile_event_seq": 4,
        "resolved_by_operator_id": "test",
        "resolution_reason": "proven",
        "resolution_evidence": evidence,
        "occurred_at_ms": 6,
    }
    if kind == "not_accepted":
        resolution = UncertaintyMarkedNotAccepted(**common, candidate_count=0)
    elif kind == "bound":
        evidence["venue_offer_id"] = "venue-1"
        resolution = UncertaintyBoundToVenueOffer(
            **common, venue_offer_id="venue-1", venue_status="active"
        )
    else:
        resolution = SubmitMatchedToVenueOffer(
            symbol="fUST",
            cid=payload.cid,
            signal_correlation_id=opening.signal_correlation_id,
            account_id=str(ACCOUNT),
            is_simulated=True,
            amount=Decimal("100"),
            occurred_at_ms=6,
            reservation_ref=replace(opening.reservation_ref, venue_offer_id="venue-1"),
            venue_offer_id="venue-1",
            venue_status="active",
            matched_mts_created=2,
            reconcile_event_seq=4,
        )
    history = tuple(
        decoded(event, seq)
        for event, seq in (
            (opening, 2),
            (unknown, 3),
            (observation, 4),
            (resolution, 5),
        )
    )
    decisions = {decision.decision_id: decision_row(decision)}
    attempts, uncertainties, _ = decode_facts(SCOPE, history, decisions, 1, 10)
    assert attempts[0].outcome == "unknown"
    assert attempts[0].resolution == ("not_sent" if kind == "not_accepted" else "acknowledged")
    assert not uncertainties[0].is_open
    with pytest.raises(EvidenceError, match="unknown_match_evidence_gap"):
        decode_facts(SCOPE, tuple(event for event in history if event.seq != 4), decisions, 1, 10)
    intervening = decoded(replace(observation, event_id=uuid4()), 5)
    with_intervening = (*history[:-1], intervening, decoded(resolution, 6))
    if kind == "match":
        assert (
            decode_facts(SCOPE, with_intervening, decisions, 1, 10)[0][0].resolution
            == "acknowledged"
        )
    else:
        with pytest.raises(EvidenceError, match="unknown_match_evidence_gap"):
            decode_facts(SCOPE, with_intervening, decisions, 1, 10)


def test_quarantine_merges_first_opening_and_only_resolution_closes():
    first = VenueOfferQuarantined(
        "orphan-1", "fUST", amount=Decimal("10"), account_id=str(ACCOUNT), occurred_at_ms=2
    )
    second = replace(first, venue_offer_id="orphan-2", event_id=uuid4(), occurred_at_ms=3)
    identity = uuid5(UUID("b1f89542-a63e-584a-b5bc-cd448ed74f3f"), str(first.event_id))
    history = (decoded(first, 1), decoded(second, 2), decoded(_snapshot(finished=5), 3))
    _, uncertainties, _ = decode_facts(SCOPE, history, {}, 0, 10)
    assert len(uncertainties) == 1 and uncertainties[0].uncertainty_id == identity
    assert uncertainties[0].is_open  # An empty venue observation does not close it.
    resolution = UncertaintyManuallyResolved(
        uncertainty_id=identity,
        account_id=str(ACCOUNT),
        environment=ENV,
        symbol="fUST",
        kind="unattributed_venue_offer",
        reconcile_event_seq=3,
        resolved_by_operator_id="test",
        resolution_reason="checked",
        resolution_action="closed_at_venue",
        occurred_at_ms=6,
        resolution_evidence={
            "reconcile_event_seq": 3,
            "query_started_at_ms": 4,
            "query_finished_at_ms": 5,
        },
    )
    assert not decode_facts(SCOPE, (*history, decoded(resolution, 4)), {}, 0, 10)[1][0].is_open


async def test_manifest_and_caps_fail_closed(monkeypatch):
    head, policy, accepted, query, row = base_rows()
    rows = [row]

    async def fake_rows(session, sql, params):
        if "current_setting" in sql:
            return [{"isolation": "repeatable read", "read_only": "on"}]
        if "capital_policy_heads" in sql:
            return [head]
        if "capital_policy_revisions" in sql:
            return [policy]
        if "capital_snapshots" in sql:
            return [accepted]
        if "capital_snapshot_queries" in sql:
            return [query]
        if "event_prefix_hashes" in sql:
            return [{"event_seq": 1, "prefix_hash": "prefix"}]
        if sql.startswith("SELECT event_seq, octet_length"):
            assert "event_type IN" in sql
            if "RESERVATION_INTENT" in params.values():
                assert "event_seq > :fence" in sql
            return [
                {"event_seq": r["event_seq"], "payload_bytes": len(canonical_bytes(r["payload"]))}
                for r in rows
                if r["event_type"] in params.values()
            ]
        if "event_log" in sql:
            if "event_type IN" in sql:
                if "RESERVATION_INTENT" in params.values():
                    assert "event_seq > :fence" in sql
                return [r for r in reversed(rows) if r["event_type"] in params.values()]
            if "event_seq = :accepted_seq" in sql:
                return [row]
            if "SELECT event_seq" in sql:
                assert "payload" not in sql
                return [{"event_seq": 1}]
            raise AssertionError(f"unbounded event payload query: {sql}")
        raise AssertionError(sql)

    monkeypatch.setattr(loader_module, "_rows", fake_rows)
    session = Mock(new=(), dirty=(), deleted=(), no_autoflush=nullcontext())
    session.in_transaction.return_value = True
    session.scalar = AsyncMock(return_value=1)
    loader = build_candidate_loader(source_revision="test", limits=ScanLimits(max_history_rows=1))
    first = await loader.load(session, scope=SCOPE, now_ms=1100, max_snapshot_age_ms=10000)
    assert isinstance(first, LoadedInputs)
    assert first.candidate_input_digest == candidate_input_digest(first.manifest.digest_payload())
    assert json.loads(first.manifest.work_json)["complete"] is True
    inputs = first.inputs
    result = derive_capital(
        scope=inputs.scope,
        policy=inputs.policy,
        accepted=inputs.accepted,
        attempts=inputs.attempts,
        uncertainties=inputs.uncertainties,
        read_context=inputs.read_context,
    )
    assert isinstance(result, Available)
    assert result.view.snapshot.total_capital == 1000  # U already included in C; foreign excluded.
    assert result.view.snapshot.cell_exposure == 50
    second = await loader.load(session, scope=SCOPE, now_ms=1100, max_snapshot_age_ms=10000)
    assert second == first
    row["payload"]["diagnostic"] = "consumed raw evidence"
    row["payload_bytes"] = len(canonical_bytes(row["payload"]))
    changed = await loader.load(session, scope=SCOPE, now_ms=1100, max_snapshot_age_ms=10000)
    assert (
        isinstance(changed, LoadedInputs)
        and changed.candidate_input_digest != first.candidate_input_digest
    )
    rows.extend(
        [
            {**row, "event_seq": 2, "event_type": "RESERVATION_INTENT"},
            {**row, "event_seq": 3, "event_type": "RESERVATION_INTENT"},
        ]
    )
    capped = await loader.load(session, scope=SCOPE, now_ms=1100, max_snapshot_age_ms=10000)
    assert isinstance(capped, NotComparable)
    assert capped.reason == "history_row_limit" and capped.classification == "input_evidence_gap"


def test_facade_exports_only_pure_contracts():
    import ast
    from pathlib import Path

    import bfx_funding_bot.modules.trading_shadow as facade

    tree = ast.parse(Path(facade.__file__).read_text())
    assert all(
        not isinstance(node, ast.ImportFrom)
        or node.module == "bfx_funding_bot.modules.trading_shadow.contracts"
        for node in tree.body
    )
    assert not any(isinstance(node, (ast.Call, ast.Await)) for node in ast.walk(tree))
