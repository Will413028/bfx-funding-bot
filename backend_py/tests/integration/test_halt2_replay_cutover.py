"""Halt 2 replay/conversion operator contracts.

The replay check is deliberately event-only: a current projection is only a
diagnostic comparison target, never an input to the rebuilt result.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.event_store.entities import VenueOfferObservation
from bfx_funding_bot.modules.execution.event_store.projector import derive_v2_event_id
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    PositionStateRow,
    VenueOfferStateRow,
)
from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from scripts.verify_projection_replay import (
    ReplayVerificationError,
    convert_pending_to_unknown,
    quarantine_orphans_after_replay,
    render_replay_report,
    replay_event_log,
    replay_one_account,
)
from tests.integration.test_orphan_quarantine_pg import _ACCOUNT as _ORPHAN_ACCOUNT
from tests.integration.test_orphan_quarantine_pg import _offer as _orphan_offer
from tests.integration.test_unknown_submit_pg import _ACCOUNT as _PENDING_ACCOUNT
from tests.integration.test_unknown_submit_pg import _seed_attempt

pytestmark = pytest.mark.integration

_ACCOUNT = UUID("00000000-0000-0000-0000-000000000043")
_ENV = "ci"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _assert_in_order(text: str, steps: tuple[str, ...]) -> None:
    """Require the human runbook's irreversible steps to remain ordered."""
    cursor = -1
    for step in steps:
        next_cursor = text.find(step, cursor + 1)
        assert next_cursor >= 0, f"missing ordered step: {step}"
        assert next_cursor > cursor, f"out-of-order step: {step}"
        cursor = next_cursor


def test_halt2_runbooks_cover_every_operator_gate_and_rollback_boundary() -> None:
    """Deleting a safety gate, report field, or rollback branch must fail review."""
    canary = (_REPOSITORY_ROOT / "docs/runbooks/halt-2-projector-canary.md").read_text()
    rollback = (_REPOSITORY_ROOT / "docs/runbooks/rollback-after-venue-write.md").read_text()
    architecture = (_REPOSITORY_ROOT / "backend_py/ARCHITECTURE.md").read_text()

    preflight_stop_reasons = (
        "legacy_environment_variable:BFX_ACCOUNT_ID",
        "missing_evidence",
        "exchange_account_id_mismatch",
        "deployment_environment_mismatch",
        "backup_evidence_hash_mismatch",
        "isolated_restore_evidence_hash_mismatch",
        "event_head_mismatch",
        "event_hash_mismatch",
        "config_digest_mismatch",
        "image_digest_mismatch",
        "migration_head_mismatch",
        "schema_heads_mismatch",
        "projector_version_mismatch",
        "open_execution_uncertainty",
        "persistent_halt_absent",
    )
    preflight_report_fields = (
        "exchange_account_id",
        "deployment_environment",
        "migration_head",
        "schema_heads",
        "backup_evidence_hash",
        "isolated_restore_evidence_hash",
        "event_count",
        "event_head",
        "event_hash",
        "open_uncertainty_count",
        "venue_snapshot_fence",
        "config_digest",
        "image_digest",
        "projector_version",
        "persistent_halt",
        "stop_reasons",
    )
    canary_report_fields = (
        "account_id",
        "environment",
        "symbol",
        "cell",
        "strategy",
        "amount_usdt",
        "command_decision_id",
        "attempt_id",
        "outcome_kind",
        "venue_offer_id",
        "outcome_at_ms",
        "reconcile_fences",
        "reconcile_observed_at_ms",
        "projection_hash",
        "venue_db_exposure_diff_usdt",
        "full_account_snapshot_complete",
        "stop_reason",
    )
    for requirement in (*preflight_stop_reasons, *preflight_report_fields, *canary_report_fields):
        assert requirement in canary, f"missing canary preflight contract: {requirement}"

    _assert_in_order(canary, (
        "uv run python scripts/halt2_cutover.py assert-halt",
        "uv run python scripts/halt2_cutover.py preflight",
        "uv run python scripts/verify_projection_replay.py replay",
        "uv run python scripts/verify_projection_replay.py convert-pending",
        "uv run python scripts/verify_projection_replay.py quarantine",
        "uv run python scripts/run_canary_preflight.py",
        "./scripts/deploy-vm.sh canary",
    ))
    for requirement in (
        "exit 0", "exit 2", "exit 3", "Operator confirmation",
        "one account", "one symbol", "one cell", "one minimal command",
        "two full-account reconcile cycles", "cannot auto-ramp", "Stop / abort",
        "API auth denial", "direct signup denial", "account-local projection hash",
        "event-chain replay", "UNKNOWN fault matrix", "orphan quarantine",
        "fresh venue full-account diff", "persistent halt effectiveness", "no automatic retry",
        "does not execute production operations", "no remote migration", "no deployment",
        "no restart", "no resume", "no cap increase", "no Bitfinex write",
        "API key", "secret", "Authorization", "raw response",
        "halt2_cutover.py replay", "exit 2", "verify_projection_replay.py",
    ):
        assert requirement in canary, f"missing canary safety requirement: {requirement}"

    for requirement in (
        "Before any venue write", "After any venue write", "image rollback", "halt",
        "reconcile", "adopt", "manual resolution", "forward-fix", "DB restore",
        "proof that no later venue mutation occurred", "persistent halt", "no automatic retry",
        "Operator confirmation", "does not execute production operations",
    ):
        assert requirement in rollback, f"missing rollback boundary: {requirement}"
    assert "Halt 2 clean cutover" in architecture
    assert "after a venue write" in architecture


def _row(*, seq: int, payload: dict[str, object] | None = None) -> EventLogRow:
    body = payload or {
        "__schema_version__": 3,
        "__event_type__": "CREDIT_CLOSED",
        "account_id": str(_ACCOUNT),
        "symbol": "fUST",
        "amount": "1",
        "occurred_at_ms": 1_000 + seq,
        "event_id": "bf0e94ae-3a03-4810-ae8f-b3c931531ce0",
    }
    return EventLogRow(
        event_seq=seq,
        account_id=str(_ACCOUNT),
        exchange_account_id=_ACCOUNT,
        deployment_environment=_ENV,
        event_type="CREDIT_CLOSED",
        payload=body,
        occurred_at_ms=1_000 + seq,
        event_id=UUID("bf0e94ae-3a03-4810-ae8f-b3c931531ce0"),
        schema_version=int(body.get("__schema_version__", 2)),
    )


def test_empty_projection_replay_has_stable_hash_and_ignores_old_projection() -> None:
    """Changing a runtime projection must not change event-only replay evidence."""
    rows = [_row(seq=1), _row(seq=2)]

    first = replay_event_log(rows, account_id=_ACCOUNT, environment=_ENV)
    second = replay_event_log(rows, account_id=_ACCOUNT, environment=_ENV)

    assert first.row_counts["event_log"] == 2
    assert first.content_hashes == second.content_hashes
    assert first.event_head == 2


@pytest.mark.asyncio
async def test_replay_uses_empty_temporary_projection_and_old_runtime_is_diagnostic_only(
    pg_session_factory,
) -> None:
    """Replacing the temporary projector with runtime reads must change this result."""
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ACCOUNT, venue="bitfinex", label="replay-empty"))
        await session.commit()

    snapshot = VenueSnapshotObserved(
        account_id=str(_ACCOUNT), environment=_ENV,
        query_started_at_ms=1_000, query_finished_at_ms=1_001,
        offers=(
            VenueOfferObservation(
                venue_offer_id="temporary-projector-offer", symbol="fUST",
                amount_original=Decimal("7"), amount_remaining=Decimal("7"),
                rate=Decimal("0.01"), period_days=2, status="active",
                mts_created=1_000, mts_updated=1_001,
            ),
        ),
        credits=(), wallet_available={"fUST": Decimal("3")},
        coverage=SnapshotCoverage(True, True, True),
    )
    async with pg_session_factory() as session:
        await PostgresEventStore(deployment_environment=_ENV).append_snapshot(session, snapshot)
        await session.commit()

    async with pg_session_factory() as session:
        runtime_position = await session.scalar(select(PositionStateRow).where(
            PositionStateRow.exchange_account_id == _ACCOUNT,
            PositionStateRow.deployment_environment == _ENV,
        ))
        assert runtime_position is not None
        runtime_position.offered_amount = Decimal("999")
        await session.commit()

    async with pg_session_factory() as session:
        report = await replay_one_account(
            session, account_id=_ACCOUNT, environment=_ENV,
            projector_version="execution-state-v1",
        )

    async with pg_session_factory() as session:
        preserved_runtime_amount = await session.scalar(select(PositionStateRow.offered_amount).where(
            PositionStateRow.exchange_account_id == _ACCOUNT,
            PositionStateRow.deployment_environment == _ENV,
        ))
    assert preserved_runtime_amount == Decimal("999")
    assert report.row_counts["venue_offer_state"] == 1
    assert report.row_counts["position_state"] == 1
    assert report.diagnostic_old_row_counts == {
        name: count for name, count in report.row_counts.items() if name != "event_log"
    }
    assert report.diagnostic_diff["position_state"]["matches"] is False
    assert report.diagnostic_diff["position_state"]["old_hash"] != (
        report.diagnostic_diff["position_state"]["replayed_hash"]
    )


@pytest.mark.asyncio
async def test_replay_is_independent_of_runtime_execution_decision_audit_state(
    pg_session_factory,
) -> None:
    """Looking up a mutated live decision would make replay non-deterministic."""
    await _seed_attempt(pg_session_factory, unknown=False)

    async with pg_session_factory() as session:
        first = await replay_one_account(
            session, account_id=_PENDING_ACCOUNT, environment=_ENV,
            projector_version="execution-state-v1",
        )

    async with pg_session_factory() as session:
        decision = await session.get(ExecutionDecisionRow, "decision-unknown")
        assert decision is not None
        decision.symbol = "fUSD"
        await session.commit()

    async with pg_session_factory() as session:
        second = await replay_one_account(
            session, account_id=_PENDING_ACCOUNT, environment=_ENV,
            projector_version="execution-state-v1",
        )

    assert second.row_counts == first.row_counts
    assert second.content_hashes == first.content_hashes


def test_replay_derives_historical_v2_uuid_and_accepts_global_sequence_gaps() -> None:
    """Treating global rows from other accounts as a corruption is unsafe."""
    payload = {"amount": "1", "account_id": str(_ACCOUNT), "is_simulated": True}
    legacy = EventLogRow(
        event_seq=1,
        account_id=str(_ACCOUNT),
        exchange_account_id=_ACCOUNT,
        deployment_environment=_ENV,
        event_type="CREDIT_CLOSED",
        payload=payload,
        occurred_at_ms=1_001,
        schema_version=2,
    )

    result = replay_event_log([legacy], account_id=_ACCOUNT, environment=_ENV)

    assert result.event_ids == (derive_v2_event_id(
        event_seq=1,
        account_id=str(_ACCOUNT),
        deployment_environment=_ENV,
        event_type="CREDIT_CLOSED",
        occurred_at_ms=1_001,
        payload=payload,
    ),)
    scoped = replay_event_log(
        [_row(seq=1), _row(seq=3)], account_id=_ACCOUNT, environment=_ENV,
    )
    assert scoped.event_head == 3
    with pytest.raises(ReplayVerificationError, match="sequence ordering"):
        replay_event_log([_row(seq=3), _row(seq=1)], account_id=_ACCOUNT, environment=_ENV)
    with pytest.raises(ReplayVerificationError, match="positive event sequence"):
        replay_event_log([_row(seq=0)], account_id=_ACCOUNT, environment=_ENV)
    missing_sequence = _row(seq=1)
    missing_sequence.event_seq = None  # type: ignore[assignment]
    with pytest.raises(ReplayVerificationError, match="missing event sequence"):
        replay_event_log([missing_sequence], account_id=_ACCOUNT, environment=_ENV)


def test_replay_rejects_unsupported_projector_version() -> None:
    """A typo must not silently select the current projector implementation."""
    with pytest.raises(ReplayVerificationError, match="unsupported projector version"):
        replay_event_log(
            [_row(seq=1)], account_id=_ACCOUNT, environment=_ENV,
            projector_version="anything-non-empty",
        )


@pytest.mark.asyncio
async def test_replay_fails_closed_when_a_stored_event_has_no_upcaster(pg_session_factory) -> None:
    """Ignoring an unknown historical schema would make a partial replay look valid."""
    async with pg_session_factory() as session:
        session.add(EventLogRow(
            account_id=str(_ACCOUNT), exchange_account_id=_ACCOUNT,
            deployment_environment=_ENV, event_type="CREDIT_CLOSED",
            payload={"__schema_version__": 1, "__event_type__": "CREDIT_CLOSED"},
            occurred_at_ms=1_000, schema_version=1,
        ))
        await session.commit()

    async with pg_session_factory() as session:
        with pytest.raises(ReplayVerificationError, match="missing or invalid historical upcaster"):
            await replay_one_account(
                session, account_id=_ACCOUNT, environment=_ENV,
                projector_version="execution-state-v1",
            )


def test_replay_rejects_invalid_identity_and_hash_mismatch() -> None:
    """A malformed event identity or supplied event hash can never be advisory."""
    row = _row(seq=1)
    malformed = _row(seq=1)
    malformed.event_id = UUID("00000000-0000-0000-0000-000000000000")

    with pytest.raises(ReplayVerificationError, match="event identity"):
        replay_event_log([malformed], account_id=_ACCOUNT, environment=_ENV)
    with pytest.raises(ReplayVerificationError, match="event hash mismatch"):
        replay_event_log([row], account_id=_ACCOUNT, environment=_ENV, expected_event_hash="bad")


def test_replay_report_is_bounded_and_never_serializes_raw_event_data() -> None:
    """Returning event ids or diagnostic payloads could disclose venue credentials."""
    report = replay_event_log([_row(seq=1)], account_id=_ACCOUNT, environment=_ENV)
    output = render_replay_report(replace(
        report,
        row_counts={**report.row_counts, "Authorization": 999},
        content_hashes={**report.content_hashes, "Authorization": "raw venue response"},
        diagnostic_old_row_counts={"Authorization": 999},
        diagnostic_old_content_hashes={"Authorization": "raw venue response"},
        diagnostic_diff={
            "Authorization": {
                "old_count": 999,
                "replayed_count": 999,
                "old_hash": "raw venue response",
                "replayed_hash": "raw venue response",
                "matches": True,
            },
        },
    ))

    assert set(output) == {
        "account_id", "environment", "projector_version", "event_head", "event_hash",
        "row_counts", "content_hashes", "diagnostic_old_row_counts",
        "diagnostic_old_content_hashes", "diagnostic_diff",
    }
    assert "event_ids" not in output
    assert "Authorization" not in str(output)
    assert "raw venue response" not in str(output)


@pytest.mark.asyncio
async def test_pending_conversion_appends_one_unknown_and_is_idempotent(pg_session_factory) -> None:
    """Replacing writer serialization with executor work would reopen submit risk."""
    await _seed_attempt(pg_session_factory, unknown=False)

    first = await convert_pending_to_unknown(
        pg_session_factory, account_id=_PENDING_ACCOUNT, environment=_ENV, now_ms=5_000,
    )
    second = await convert_pending_to_unknown(
        pg_session_factory, account_id=_PENDING_ACCOUNT, environment=_ENV, now_ms=6_000,
    )

    async with pg_session_factory() as session:
        event_types = list(await session.scalars(select(EventLogRow.event_type).where(
            EventLogRow.exchange_account_id == _PENDING_ACCOUNT,
        ).order_by(EventLogRow.event_seq)))
        uncertainty = await session.scalar(select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.exchange_account_id == _PENDING_ACCOUNT,
            ExecutionUncertaintyRow.state == "open",
        ))
    assert first == 1
    assert second == 0
    assert event_types.count("SUBMIT_OUTCOME_UNKNOWN") == 1
    assert uncertainty is not None and uncertainty.kind == "submit_outcome_unknown"


@pytest.mark.asyncio
async def test_replayed_orphan_is_quarantined_without_losing_venue_object(pg_session_factory) -> None:
    """Skipping quarantine after a clean replay would silently un-block an orphan."""
    async with pg_session_factory() as session:
        session.add(ExchangeAccount(id=_ORPHAN_ACCOUNT, venue="bitfinex", label="replay-orphan"))
        await session.commit()
    orphan = _orphan_offer("replay-orphan", "fXYZ", "7")
    snapshot = VenueSnapshotObserved(
        account_id=str(_ORPHAN_ACCOUNT), environment=_ENV,
        query_started_at_ms=1_000, query_finished_at_ms=1_001,
        offers=(
            VenueOfferObservation(
                venue_offer_id=orphan.venue_offer_id, symbol=orphan.symbol,
                amount_original=orphan.amount_original, amount_remaining=orphan.amount,
                rate=orphan.rate_decimal, period_days=orphan.period_days, status=orphan.status,
                mts_created=orphan.mts_created, mts_updated=orphan.mts_updated,
                offer_type=orphan.offer_type, flags={},
            ),
        ),
        credits=(), wallet_available={"fXYZ": orphan.amount},
        coverage=SnapshotCoverage(True, True, True),
    )
    async with pg_session_factory() as session:
        await PostgresEventStore(deployment_environment=_ENV).append_snapshot(session, snapshot)
        await session.commit()

    first = await quarantine_orphans_after_replay(
        pg_session_factory, account_id=_ORPHAN_ACCOUNT, environment=_ENV, now_ms=2_000,
    )
    second = await quarantine_orphans_after_replay(
        pg_session_factory, account_id=_ORPHAN_ACCOUNT, environment=_ENV, now_ms=3_000,
    )

    async with pg_session_factory() as session:
        venue = await session.get(
            VenueOfferStateRow,
            (_ORPHAN_ACCOUNT, _ENV, "replay-orphan"),
        )
        uncertainty = await session.scalar(select(ExecutionUncertaintyRow).where(
            ExecutionUncertaintyRow.exchange_account_id == _ORPHAN_ACCOUNT,
            ExecutionUncertaintyRow.state == "open",
        ))
    assert first == 1
    assert second == 0
    assert venue is not None and venue.amount_remaining == orphan.amount
    assert uncertainty is not None and uncertainty.kind == "unattributed_venue_offer"
