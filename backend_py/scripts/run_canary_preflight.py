"""Read-only verifier for one bounded Halt 2 real-money canary.

This command never resumes trading, writes a database row, or calls Bitfinex.
It consumes the existing Halt 2 artifact contract plus event-only replay output
and emits only redacted, account-local readiness facts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.event_store.serialization import deserialize_stored_event
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed, VenueSnapshotObserved
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitOutcomeKind,
    fingerprint_submit_payload,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    CanaryCommandPermitRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.marketfeed.daemon import (
    CanaryEvidence,
    CanaryProfile,
    CanaryStartupBlocked,
    assert_canary_pre_command,
    assert_canary_startup,
    assert_release_observation,
    assert_release_pre_command,
    collect_canary_readiness,
)

# Direct ``python scripts/...`` execution otherwise exposes scripts/ rather
# than its parent package. This only resolves local source imports; it does not
# affect evidence inputs or runtime configuration.
if __package__ in {None, ""}:  # pragma: no cover - operator CLI path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.halt2_cutover import (
    Halt2Evidence,
    _artifact_hashes,
    collect_preflight_report,
    verify_preflight,
)
from scripts.verify_projection_replay import (
    ReplayReport,
    ReplayVerificationError,
    replay_one_account,
)

EXIT_SUCCESS = 0
EXIT_PRECONDITION_FAILED = 2
EXIT_VERIFICATION_FAILED = 3


@dataclass(frozen=True)
class CanaryAttemptSelector:
    """IDs from the consumed session, never a human-supplied outcome claim."""
    permit_id: str
    attempt_id: str
    command_decision_id: str


def _assert_claim_matches_server(
    claim: CanaryEvidence,
    server_derived: CanaryEvidence,
) -> None:
    """Reject any operator JSON field that is not reproduced from durable rows."""
    mismatches = tuple(
        field.name
        for field in fields(CanaryEvidence)
        if getattr(claim, field.name) != getattr(server_derived, field.name)
    )
    if mismatches:
        # Keep the refusal bounded: field names are safe to print, values may
        # contain venue/account evidence and must never be echoed.
        raise CanaryStartupBlocked(
            "canary_evidence_not_server_derived:" + ",".join(mismatches)
        )


async def _projection_hash(
    session: AsyncSession,
    *,
    profile: CanaryProfile,
    projector_version: str,
) -> tuple[str, ReplayReport]:
    """Hash the complete empty-projector result; old rows remain diagnostics."""
    replay = await replay_one_account(
        session,
        account_id=profile.account_id,
        environment=profile.environment,
        projector_version=projector_version,
    )
    return hashlib.sha256(
        json.dumps(replay.content_hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest(), replay


def _snapshot_exposure(snapshot: VenueSnapshotObserved) -> Decimal:
    """Return the account-wide exposure carried by one venue observation."""
    return sum(
        (
            [Decimal(str(offer.amount_remaining)) for offer in snapshot.offers]
            + [Decimal(str(credit.amount)) for credit in snapshot.credits]
        ),
        Decimal("0"),
    )


async def _derive_canary_evidence_from_durable_rows(
    *,
    session: AsyncSession,
    profile: CanaryProfile,
    projector_version: str,
    now_ms: int,
    claim: CanaryEvidence | CanaryAttemptSelector,
) -> CanaryEvidence:
    """Build canary facts from the attempt, outcome event, replay, and snapshots."""
    # The report's IDs are only selectors.  Every admitted value below is read
    # from durable rows and the event-only replay, never from the JSON claim.
    try:
        attempt_id = UUID(str(claim.attempt_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CanaryStartupBlocked("invalid_canary_attempt_id") from exc
    attempt = await session.scalar(
        select(SubmissionAttemptRow).where(
            SubmissionAttemptRow.attempt_id == attempt_id,
            SubmissionAttemptRow.execution_decision_id == claim.command_decision_id,
            SubmissionAttemptRow.exchange_account_id == profile.account_id,
            SubmissionAttemptRow.deployment_environment == profile.environment,
            SubmissionAttemptRow.symbol == profile.symbol,
        )
    )
    if attempt is None:
        raise CanaryStartupBlocked("canary_durable_attempt_missing")
    try:
        permit_id = UUID(str(claim.permit_id))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CanaryStartupBlocked("invalid_canary_permit_id") from exc
    permit = await session.scalar(
        select(CanaryCommandPermitRow).where(
            CanaryCommandPermitRow.permit_id == permit_id,
            CanaryCommandPermitRow.halt_id.is_not(None),
            CanaryCommandPermitRow.exchange_account_id == profile.account_id,
            CanaryCommandPermitRow.deployment_environment == profile.environment,
            CanaryCommandPermitRow.symbol == profile.symbol,
            CanaryCommandPermitRow.cell == profile.cell,
            CanaryCommandPermitRow.strategy == profile.strategy,
            CanaryCommandPermitRow.state == "consumed",
            CanaryCommandPermitRow.execution_decision_id == attempt.execution_decision_id,
        )
    )
    if permit is None:
        raise CanaryStartupBlocked("canary_durable_permit_missing_or_unbound")
    if (
        attempt.outcome_kind != SubmitOutcomeKind.ACKNOWLEDGED.value
        or not attempt.venue_offer_id
        or attempt.completed_at_ms is None
        or attempt.last_event_seq is None
    ):
        raise CanaryStartupBlocked("canary_durable_outcome_incomplete")

    decision_result = await session.execute(
        select(ExecutionDecisionRow.cell_id, ExecutionDecisionRow.amount_usdt, ExecutionDecisionRow.strategy).where(
            ExecutionDecisionRow.decision_id == attempt.execution_decision_id,
            ExecutionDecisionRow.exchange_account_id == profile.account_id,
            ExecutionDecisionRow.deployment_environment == profile.environment,
            ExecutionDecisionRow.symbol == profile.symbol,
            ExecutionDecisionRow.cell_id == profile.cell,
        )
    )
    decision = decision_result.first()
    if decision is None:
        raise CanaryStartupBlocked("canary_durable_decision_missing")
    payload = attempt.normalized_payload
    if not isinstance(payload, Mapping):
        raise CanaryStartupBlocked("canary_durable_payload_invalid")
    try:
        amount = Decimal(str(payload["amount"]))
        if fingerprint_submit_payload(payload) != attempt.payload_sha256:
            raise ValueError("payload fingerprint mismatch")
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise CanaryStartupBlocked("canary_durable_payload_invalid") from exc
    if (
        payload.get("symbol") != profile.symbol
        or amount != profile.amount_usdt
        or amount > profile.cap_usdt
        or Decimal(str(decision.amount_usdt)) != amount
        or decision.cell_id != profile.cell
        or (isinstance(claim, CanaryAttemptSelector) and decision.strategy != profile.strategy)
    ):
        raise CanaryStartupBlocked("canary_durable_scope_mismatch")

    outcome_row = await session.scalar(
        select(EventLogRow).where(
            EventLogRow.event_seq == attempt.last_event_seq,
            EventLogRow.exchange_account_id == profile.account_id,
            EventLogRow.deployment_environment == profile.environment,
        )
    )
    if outcome_row is None:
        raise CanaryStartupBlocked("canary_outcome_event_missing")
    try:
        outcome_event = deserialize_stored_event(outcome_row)
    except (TypeError, ValueError) as exc:
        raise CanaryStartupBlocked("canary_outcome_event_invalid") from exc
    if not isinstance(outcome_event, ReservationClaimed) or (
        outcome_event.account_id != str(profile.account_id)
        or outcome_event.symbol != profile.symbol
        or outcome_event.cid != attempt.cid
        or outcome_event.venue_offer_id != attempt.venue_offer_id
        or outcome_event.reservation_ref is None
        or outcome_event.reservation_ref.execution_decision_id != attempt.execution_decision_id
    ):
        raise CanaryStartupBlocked("canary_outcome_event_mismatch")

    try:
        projection_hash, replay = await _projection_hash(
            session,
            profile=profile,
            projector_version=projector_version,
        )
    except (ReplayVerificationError, RuntimeError, ValueError) as exc:
        raise CanaryStartupBlocked("canary_replay_unavailable") from exc
    readiness = await collect_canary_readiness(
        session,
        account_id=profile.account_id,
        environment=profile.environment,
        now_ms=now_ms,
        after_event_seq=attempt.last_event_seq,
        minimum_snapshot_count=2,
    )
    latest_snapshot_row = await session.scalar(
        select(EventLogRow)
        .where(
            EventLogRow.exchange_account_id == profile.account_id,
            EventLogRow.deployment_environment == profile.environment,
            EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED",
            EventLogRow.event_seq > attempt.last_event_seq,
        )
        .order_by(EventLogRow.event_seq.desc())
        .limit(1)
    )
    latest_snapshot: VenueSnapshotObserved | None = None
    if latest_snapshot_row is not None:
        try:
            decoded = deserialize_stored_event(latest_snapshot_row)
        except (TypeError, ValueError) as exc:
            raise CanaryStartupBlocked("canary_snapshot_event_invalid") from exc
        if isinstance(decoded, VenueSnapshotObserved):
            latest_snapshot = decoded
    if latest_snapshot is None:
        raise CanaryStartupBlocked("canary_snapshot_event_missing")
    replay_exposure = sum(
        replay.replayed_offer_exposure_by_symbol.values(), Decimal("0")
    ) + sum(replay.replayed_credit_exposure_by_symbol.values(), Decimal("0"))
    exposure_diff = _snapshot_exposure(latest_snapshot) - replay_exposure
    return CanaryEvidence(
        account_id=str(profile.account_id),
        environment=profile.environment,
        symbol=profile.symbol,
        cell=profile.cell,
        strategy=profile.strategy,
        amount_usdt=amount,
        permit_id=str(permit.permit_id),
        command_decision_id=attempt.execution_decision_id,
        attempt_id=str(attempt.attempt_id),
        outcome_kind=attempt.outcome_kind,
        venue_offer_id=attempt.venue_offer_id,
        outcome_at_ms=attempt.completed_at_ms,
        outcome_event_seq=attempt.last_event_seq,
        reconcile_fences=readiness.reconcile_fences,
        reconcile_observed_at_ms=readiness.reconcile_observed_at_ms,
        projection_hash=projection_hash,
        venue_db_exposure_diff_usdt=exposure_diff,
        full_account_snapshot_complete=readiness.full_account_snapshot_complete,
        stop_reason=None,
    )


def _parse_canary_permit_id(environ: Mapping[str, str]) -> UUID:
    raw = environ.get("BFX_CANARY_PERMIT_ID", "").strip()
    if not raw:
        raise CanaryStartupBlocked("missing_canary_permit_id")
    try:
        return UUID(raw)
    except ValueError as exc:
        raise CanaryStartupBlocked("invalid_canary_permit_id") from exc


async def _assert_issued_canary_permit(
    session: AsyncSession,
    *,
    profile: CanaryProfile,
    permit_id: UUID,
) -> None:
    """Require one unconsumed permit tied to the current durable halt epoch."""
    permit = await session.scalar(
        select(CanaryCommandPermitRow).where(
            CanaryCommandPermitRow.permit_id == permit_id,
            CanaryCommandPermitRow.exchange_account_id == profile.account_id,
            CanaryCommandPermitRow.deployment_environment == profile.environment,
            CanaryCommandPermitRow.symbol == profile.symbol,
            CanaryCommandPermitRow.cell == profile.cell,
            CanaryCommandPermitRow.strategy == profile.strategy,
            CanaryCommandPermitRow.state == "issued",
        )
    )
    if permit is None or Decimal(str(permit.amount_usdt)) != profile.amount_usdt:
        raise CanaryStartupBlocked("canary_permit_missing_or_scope_mismatch")
    current_halt = await session.execute(
        select(TradingHaltRow.id, TradingHaltRow.halted)
        .where(
            TradingHaltRow.exchange_account_id == profile.account_id,
            TradingHaltRow.deployment_environment == profile.environment,
        )
        .order_by(TradingHaltRow.id.desc())
        .limit(1)
    )
    halt = current_halt.first()
    if halt is None or halt.id != permit.halt_id or halt.halted is not True:
        raise CanaryStartupBlocked("canary_permit_halt_mismatch")


def _require_halt2_artifacts(evidence: Halt2Evidence, config_artifact: Path) -> None:
    # Task 2 owns the artifact schema and its digest implementation. Reusing it
    # here prevents this canary check from creating a weaker parallel contract.
    observed = _artifact_hashes(evidence, config_artifact=config_artifact)
    if (
        observed["backup_evidence_hash"] != evidence.backup_evidence_hash
        or observed["isolated_restore_evidence_hash"] != evidence.isolated_restore_evidence_hash
        or observed["config_digest"] != evidence.config_digest
    ):
        raise CanaryStartupBlocked("halt2_artifact_evidence_mismatch")


async def _run(args: argparse.Namespace) -> CanaryEvidence:
    # Keep typed verifier functions for the authenticated worker and historical
    # fixtures. Legacy CLI/env money and scope are no longer runtime inputs.
    raise CanaryStartupBlocked("legacy_canary_requires_authenticated_release_session")


async def verify_canary_preflight(
    *,
    session: AsyncSession,
    profile: CanaryProfile,
    halt2_evidence: Halt2Evidence,
    config_artifact: Path,
    image_digest: str,
    projector_version: str,
    environ: Mapping[str, str],
    evidence: CanaryEvidence | None,
    configured_cells: tuple[tuple[str, str, str], ...],
    configured_caps: Mapping[str, Decimal],
    allocation_cap_usdt: Decimal,
    now_ms: int,
) -> CanaryEvidence | None:
    """One shared, read-only authority for CLI and daemon canary admission."""
    if (
        halt2_evidence.exchange_account_id != str(profile.account_id)
        or halt2_evidence.deployment_environment != profile.environment
    ):
        raise CanaryStartupBlocked("halt2_identity_or_environment_mismatch")
    _require_halt2_artifacts(halt2_evidence, config_artifact)
    report = await collect_preflight_report(
        session,
        account_id=profile.account_id,
        environment=profile.environment,
        artifact_hashes=_artifact_hashes(halt2_evidence, config_artifact=config_artifact),
        image_digest=image_digest,
        projector_version=projector_version,
    )
    halt2_result = verify_preflight(report, halt2_evidence, environ=environ)
    if halt2_result.stop_reasons:
        raise CanaryStartupBlocked("halt2_preflight:" + ",".join(halt2_result.stop_reasons))
    if evidence is None:
        await _assert_issued_canary_permit(
            session,
            profile=profile,
            permit_id=_parse_canary_permit_id(environ),
        )
        assert_canary_pre_command(
            profile=profile,
            readiness=await collect_canary_readiness(
                session,
                account_id=profile.account_id,
                environment=profile.environment,
                now_ms=now_ms,
                minimum_snapshot_count=1,
            ),
            configured_cells=configured_cells,
            configured_caps=configured_caps,
            allocation_cap_usdt=allocation_cap_usdt,
        )
        return None
    expected_permit_id = _parse_canary_permit_id(environ)
    if evidence.permit_id != str(expected_permit_id):
        raise CanaryStartupBlocked("canary_permit_claim_mismatch")
    server_evidence = await _derive_canary_evidence_from_durable_rows(
        session=session,
        profile=profile,
        projector_version=projector_version,
        now_ms=now_ms,
        claim=evidence,
    )
    _assert_claim_matches_server(evidence, server_evidence)
    assert_canary_startup(
        profile=profile,
        evidence=server_evidence,
        readiness=await collect_canary_readiness(
            session,
            account_id=profile.account_id,
            environment=profile.environment,
            now_ms=now_ms,
            after_event_seq=server_evidence.outcome_event_seq,
            minimum_snapshot_count=2,
        ),
        configured_cells=configured_cells,
        configured_caps=configured_caps,
        allocation_cap_usdt=allocation_cap_usdt,
    )
    return server_evidence


async def verify_release_preflight(
    *, session: AsyncSession, profile: CanaryProfile, halt2_evidence: Halt2Evidence,
    config_artifact: Path, image_digest: str, projector_version: str, now_ms: int,
    selector: CanaryAttemptSelector | None = None,
) -> CanaryEvidence | None:
    """Typed session admission/observation with unchanged DR and replay gates.

The restored baseline is an immutable PREFIX of a now-advancing account stream.
Verify that prefix independently before checking current snapshots. Exact-head
equality would permanently invalidate evidence as soon as reconciliation ran.
"""
    from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash

    if (halt2_evidence.exchange_account_id != str(profile.account_id)
        or halt2_evidence.deployment_environment != profile.environment):
        raise CanaryStartupBlocked("halt2_identity_or_environment_mismatch")
    _require_halt2_artifacts(halt2_evidence, config_artifact)
    report = await collect_preflight_report(session, account_id=profile.account_id,
        environment=profile.environment, artifact_hashes=_artifact_hashes(halt2_evidence, config_artifact=config_artifact),
        image_digest=image_digest, projector_version=projector_version)
    if halt2_evidence.event_head is None:
        raise CanaryStartupBlocked("halt2_baseline_head_missing")
    prefix = list(await session.scalars(select(EventLogRow).where(
        EventLogRow.exchange_account_id == profile.account_id,
        EventLogRow.deployment_environment == profile.environment,
        EventLogRow.event_seq <= halt2_evidence.event_head,
    ).order_by(EventLogRow.event_seq)))
    if (not prefix or prefix[-1].event_seq != halt2_evidence.event_head
        or canonical_event_hash(prefix) != halt2_evidence.event_hash):
        raise CanaryStartupBlocked("halt2_event_continuity_mismatch")
    # Only current stream/snapshot fields advance after independently proven
    # continuity. DR/config/image/schema/projector identities are NOT replaced.
    continued = replace(halt2_evidence, event_head=report.event_head, event_hash=report.event_hash,
        venue_snapshot_fence=report.venue_snapshot_fence,
        venue_snapshot_observed_at_ms=report.venue_snapshot_observed_at_ms,
        venue_snapshot_complete=report.venue_snapshot_complete)
    result = verify_preflight(report, continued, environ={
        "BFX_HALT2_MAX_SNAPSHOT_AGE_SECONDS": str(profile.max_evidence_age_seconds),
    })
    if result.stop_reasons:
        raise CanaryStartupBlocked("halt2_preflight:" + ",".join(result.stop_reasons))
    if selector is None:
        assert_release_pre_command(await collect_canary_readiness(session,
            account_id=profile.account_id, environment=profile.environment,
            now_ms=now_ms, minimum_snapshot_count=1))
        return None
    evidence = await _derive_canary_evidence_from_durable_rows(session=session, profile=profile,
        projector_version=projector_version, now_ms=now_ms, claim=selector)
    readiness = await collect_canary_readiness(session, account_id=profile.account_id,
        environment=profile.environment, now_ms=now_ms, after_event_seq=evidence.outcome_event_seq,
        minimum_snapshot_count=2)
    assert_release_observation(profile=profile, evidence=evidence, readiness=readiness)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--halt2-evidence", type=Path, required=True)
    parser.add_argument("--config-artifact", type=Path, required=True)
    parser.add_argument("--cells", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # argparse inputs are intentionally not emitted; command lines may contain
    # protected filesystem layout and all useful observations are in evidence.
    args.environ = os.environ
    try:
        evidence = asyncio.run(_run(args))
    except CanaryStartupBlocked as exc:
        print(json.dumps({"stop_reasons": str(exc).split(",")}, sort_keys=True))
        return EXIT_PRECONDITION_FAILED
    except Exception:
        print(json.dumps({"stop_reasons": ["verification_unavailable"]}, sort_keys=True))
        return EXIT_VERIFICATION_FAILED
    print(json.dumps(asdict(evidence), sort_keys=True, default=str))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
