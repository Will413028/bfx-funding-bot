"""Differential values recorded against the unchanged pre-S1-3c4a router."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from operator_evidence_baseline import baseline_context
from sqlalchemy import select
from test_uncertainties_router import (
    ACCOUNT_ID,
    _append_snapshot,
    _seed_orphan_uncertainty,
    uncertainty_app,  # noqa: F401 - fixture re-export
)

from bfx_funding_bot.modules.api.uncertainties import _resolution_context
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import VenueOfferObservation
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow


def offer(identifier, *, complete=True):
    return VenueOfferObservation(
        venue_offer_id=identifier,
        symbol="fUST",
        amount_original=Decimal("100"),
        amount_remaining=Decimal("100"),
        rate=Decimal("0.001"),
        period_days=2,
        status="active",
        mts_created=1500,
        mts_updated=1500,
        offer_type="LIMIT" if complete else None,
        flags={"raw": 0},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scene,count,ids,reason",
    [
        ("stale", None, [], "stale_reconcile_fence"),
        ("incomplete_history", None, [], "incomplete_offer_history_coverage"),
        ("incomplete_match", None, [], "incomplete_match_evidence"),
        ("multiple", 2, ["a", "b"], "multiple_exact_candidates"),
        ("zero", 0, [], None),
        ("manual", None, [], None),
    ],
)
async def test_legacy_context_differential(uncertainty_app, scene, count, ids, reason):  # noqa: F811
    client, factory = uncertainty_app
    subject_id = client.uncertainty_id
    if scene == "manual":
        subject_id = await _seed_orphan_uncertainty(factory, offer("manual"))
    offers = (offer("b"), offer("a")) if scene == "multiple" else ()
    if scene == "incomplete_match":
        offers = (offer("a", complete=False),)
    finished = 3000 if scene == "manual" else 2000
    started = 1090 if scene == "stale" else finished - 10
    seq = await _append_snapshot(factory, finished_at=finished, started_at=started, offers=offers)
    if scene == "incomplete_history":
        async with factory.begin() as session:
            snapshot = await session.scalar(select(EventLogRow).where(EventLogRow.event_seq == seq))
            snapshot.payload = {
                **snapshot.payload,
                "coverage": {
                    **snapshot.payload["coverage"],
                    "offer_history_complete": False,
                },
            }
    context = SimpleNamespace(exchange_account_id=ACCOUNT_ID, deployment_environment="ci")
    async with factory() as session:
        row = await session.scalar(
            select(ExecutionUncertaintyRow).where(
                ExecutionUncertaintyRow.uncertainty_id == subject_id,
            )
        )
        expected = {
            "reconcileEventSeq": seq,
            "queryStartedAtMs": started,
            "queryFinishedAtMs": finished,
            "candidateCount": count,
            "candidateVenueOfferIds": ids,
            "unavailableReason": reason,
        }
        assert (await baseline_context(session, context=context, row=row)).model_dump(
            by_alias=True
        ) == expected
        # S1-3c4b wire: the same fence as an opaque decimal string.
        renamed = {k: v for k, v in expected.items() if k != "reconcileEventSeq"}
        renamed["evidenceRef"] = None if seq is None else str(seq)
        assert (await _resolution_context(session, context=context, row=row)).model_dump(
            by_alias=True
        ) == renamed


@pytest.mark.parametrize(
    "token",
    [
        "042",
        " 42",
        "+42",
        "4.2",
        "-42",
        "４２",
        "ledger:v1:obs:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    ],
)
def test_legacy_token_parser_rejects_noncanonical_integer_and_ledger_tokens(token):
    from bfx_funding_bot.modules.execution.operator_evidence import legacy_evidence_seq
    from bfx_funding_bot.modules.ledger import ResolutionRejected

    with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
        legacy_evidence_seq(token)


@pytest.mark.parametrize("token", ["0", "42"])
def test_legacy_token_parser_accepts_canonical_integers(token):
    from bfx_funding_bot.modules.execution.operator_evidence import legacy_evidence_seq

    assert legacy_evidence_seq(token) == int(token)


@pytest.mark.asyncio
async def test_legacy_adapter_rejects_old_fence_after_newer_partial_snapshot(uncertainty_app):  # noqa: F811
    from bfx_funding_bot.modules.execution.operator_evidence import LegacyOperatorEvidence
    from bfx_funding_bot.modules.ledger import ResolutionRejected, ResolutionSubject, Scope

    client, factory = uncertainty_app
    old = await _append_snapshot(factory, finished_at=2000)
    newer = await _append_snapshot(factory, finished_at=2000)
    async with factory.begin() as session:
        snapshot = await session.scalar(select(EventLogRow).where(EventLogRow.event_seq == newer))
        snapshot.payload = {**snapshot.payload, "coverage": {}}
    async with factory() as session:
        row = await session.scalar(
            select(ExecutionUncertaintyRow).where(
                ExecutionUncertaintyRow.uncertainty_id == client.uncertainty_id
            )
        )
        with pytest.raises(ResolutionRejected, match="stale_reconcile_fence"):
            await LegacyOperatorEvidence().verify(
                session,
                Scope(ACCOUNT_ID, "ci"),
                ResolutionSubject(row.uncertainty_id, row.symbol, row.attempt_id),
                str(old),
                require_history=True,
            )


@pytest.mark.asyncio
async def test_worker_reverifies_injected_port_under_account_lock(uncertainty_app, monkeypatch):  # noqa: F811
    from test_uncertainties_router import _Authority

    from bfx_funding_bot.modules.execution import operator_requests
    from bfx_funding_bot.modules.execution.operator_evidence import LegacyOperatorEvidence
    from bfx_funding_bot.modules.execution.uncertainty_resolution import (
        LegacyOperatorResolution,
        ResolutionScope,
        UncertaintyResolutionRequests,
        UncertaintyResolutionWorker,
    )
    from bfx_funding_bot.modules.ledger import ResolutionIntent

    client, factory = uncertainty_app
    seq = await _append_snapshot(factory, finished_at=2000)
    scope = ResolutionScope(ACCOUNT_ID, "ci")
    calls = []

    class RecordingPort(LegacyOperatorEvidence):
        async def verify(self, session, scope, subject, evidence_ref, *, require_history):
            calls.append(session.info.get("account_locked", False))
            return await super().verify(
                session, scope, subject, evidence_ref, require_history=require_history
            )

    async def mark_lock(session, **kwargs):
        session.info["account_locked"] = True

    monkeypatch.setattr(operator_requests, "acquire_transaction_lock", mark_lock)
    port = LegacyOperatorResolution(RecordingPort())
    async with factory.begin() as session:
        await UncertaintyResolutionRequests(scope, port).request(
            session,
            ResolutionIntent(client.uncertainty_id, "mark_not_accepted", str(seq), "operator-1"),
            now_ms=3000,
        )
    assert calls == [False]
    worker = UncertaintyResolutionWorker(
        session_factory=factory,
        scope=scope,
        authority=_Authority(),
        clock=lambda: 4000,
        resolution=port,
    )
    assert await worker.tick()
    assert calls == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["42", "ledger:v1:obs:00000000-0000-0000-0000-0000000000aa"])
async def test_api_passes_opaque_evidence_ref_through_unchanged(ref):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from bfx_funding_bot.modules.ledger import ResolutionEvidence, ResolutionSubject, Scope

    port = AsyncMock()
    port.resolution_context.return_value = ResolutionEvidence(ref, 100, 110, ("offer",), 1)
    context = SimpleNamespace(exchange_account_id=ACCOUNT_ID, deployment_environment="ci")
    row = SimpleNamespace(uncertainty_id=uuid4(), symbol="fUST", attempt_id=uuid4())
    session = AsyncMock()
    result = await _resolution_context(session, context=context, row=row, evidence=port)
    assert result.model_dump(by_alias=True) == {
        "evidenceRef": ref,
        "queryStartedAtMs": 100,
        "queryFinishedAtMs": 110,
        "candidateVenueOfferIds": ["offer"],
        "candidateCount": 1,
        "unavailableReason": None,
    }
    port.resolution_context.assert_awaited_once_with(
        session,
        Scope(ACCOUNT_ID, "ci"),
        ResolutionSubject(row.uncertainty_id, "fUST", row.attempt_id),
    )
