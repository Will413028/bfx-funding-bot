"""Read-only Halt 2 evidence preflight and explicit durable-halt command.

Run from ``backend_py/``.  ``preflight`` is the default and never writes to
the database or a venue.  Mutating cutover actions remain explicit operator
commands owned by later Halt 2 tasks; this command only appends a halt when
``assert-halt`` is selected.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.execution.event_store.canonical import canonical_event_hash
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, ProjectionHeadRow
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow

EXIT_SUCCESS = 0
EXIT_PRECONDITION_FAILED = 2
EXIT_VERIFICATION_FAILED = 3

_LEGACY_ENVIRONMENT_VARIABLES = ("BFX_ACCOUNT_ID",)


@dataclass(frozen=True, slots=True)
class Halt2Evidence:
    exchange_account_id: str
    deployment_environment: str
    backup_evidence_hash: str
    isolated_restore_evidence_hash: str
    event_head: int | None
    event_hash: str
    venue_snapshot_fence: int | None
    venue_snapshot_observed_at_ms: int | None
    venue_snapshot_complete: bool
    preflight_observed_at_ms: int
    backup_rpo_seconds: int | None
    restore_rto_seconds: int | None
    config_digest: str
    image_digest: str
    projector_version: str
    migration_head: str | None
    schema_heads: tuple[str, ...]
    backup_evidence_path: str
    isolated_restore_evidence_path: str
    config_artifact_path: str

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Halt2Evidence:
        required = tuple(field for field in cls.__dataclass_fields__)
        missing = tuple(name for name in required if name not in value)
        if missing:
            raise ValueError("evidence missing: " + ", ".join(missing))
        payload = {name: value[name] for name in required}
        schema_heads = payload["schema_heads"]
        if not isinstance(schema_heads, list | tuple) or isinstance(schema_heads, str):
            raise ValueError("evidence schema_heads must be an array")
        payload["schema_heads"] = tuple(str(head) for head in schema_heads)
        return cls(**payload)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class PreflightReport:
    exchange_account_id: str
    deployment_environment: str
    migration_head: str | None
    schema_heads: tuple[str, ...]
    backup_evidence_hash: str
    isolated_restore_evidence_hash: str
    event_count: int
    event_head: int | None
    event_hash: str
    open_uncertainty_count: int
    venue_snapshot_fence: int | None
    venue_snapshot_observed_at_ms: int | None
    venue_snapshot_complete: bool
    preflight_observed_at_ms: int
    backup_rpo_seconds: int | None
    restore_rto_seconds: int | None
    config_digest: str
    image_digest: str
    projector_version: str | None
    persistent_halt: bool
    stop_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreflightResult:
    exit_code: int
    stop_reasons: tuple[str, ...]
    report: PreflightReport


def _validated_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError("exchange_account_id must be a UUID") from exc


def _legacy_reasons(environ: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        f"legacy_environment_variable:{name}"
        for name in _LEGACY_ENVIRONMENT_VARIABLES
        if environ.get(name, "").strip()
    )


def verify_preflight(
    report: PreflightReport,
    evidence: Halt2Evidence | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> PreflightResult:
    """Compare immutable evidence with one freshly read report without writes."""
    env = os.environ if environ is None else environ
    reasons = list(_legacy_reasons(env))
    if evidence is None:
        reasons.append("missing_evidence")
    else:
        if evidence.exchange_account_id != report.exchange_account_id:
            reasons.append("exchange_account_id_mismatch")
        if evidence.deployment_environment != report.deployment_environment:
            reasons.append("deployment_environment_mismatch")
        if evidence.backup_evidence_hash != report.backup_evidence_hash:
            reasons.append("backup_evidence_hash_mismatch")
        if evidence.isolated_restore_evidence_hash != report.isolated_restore_evidence_hash:
            reasons.append("isolated_restore_evidence_hash_mismatch")
        if evidence.event_head != report.event_head:
            reasons.append("event_head_mismatch")
        if evidence.event_hash != report.event_hash:
            reasons.append("event_hash_mismatch")
        if evidence.venue_snapshot_fence != report.venue_snapshot_fence:
            reasons.append("venue_snapshot_fence_mismatch")
        if evidence.venue_snapshot_observed_at_ms != report.venue_snapshot_observed_at_ms:
            reasons.append("venue_snapshot_timestamp_mismatch")
        if evidence.venue_snapshot_complete != report.venue_snapshot_complete:
            reasons.append("venue_snapshot_coverage_mismatch")
        if evidence.preflight_observed_at_ms > report.preflight_observed_at_ms:
            reasons.append("preflight_timestamp_mismatch")
        if evidence.backup_rpo_seconds != report.backup_rpo_seconds:
            reasons.append("backup_rpo_mismatch")
        if evidence.restore_rto_seconds != report.restore_rto_seconds:
            reasons.append("restore_rto_mismatch")
        if evidence.config_digest != report.config_digest:
            reasons.append("config_digest_mismatch")
        if evidence.image_digest != report.image_digest:
            reasons.append("image_digest_mismatch")
        if evidence.migration_head != report.migration_head:
            reasons.append("migration_head_mismatch")
        if evidence.schema_heads != report.schema_heads:
            reasons.append("schema_heads_mismatch")
        if not evidence.projector_version.strip() or evidence.projector_version != report.projector_version:
            reasons.append("projector_version_mismatch")
    if report.open_uncertainty_count:
        reasons.append("open_execution_uncertainty")
    if report.venue_snapshot_fence is None:
        reasons.append("venue_snapshot_fence_absent")
    if report.venue_snapshot_observed_at_ms is None:
        reasons.append("venue_snapshot_timestamp_absent")
    if not report.venue_snapshot_complete:
        reasons.append("venue_snapshot_coverage_incomplete")
    max_snapshot_age_seconds = _max_snapshot_age_seconds(env)
    if (
        report.venue_snapshot_observed_at_ms is not None
        and report.preflight_observed_at_ms - report.venue_snapshot_observed_at_ms
        > max_snapshot_age_seconds * 1000
    ):
        reasons.append("venue_snapshot_stale")
    if report.backup_rpo_seconds is None:
        reasons.append("backup_rpo_unmeasured")
    elif report.backup_rpo_seconds > 300:
        reasons.append("backup_rpo_exceeded")
    if report.restore_rto_seconds is None:
        reasons.append("restore_rto_unmeasured")
    elif report.restore_rto_seconds > 60:
        reasons.append("restore_rto_exceeded")
    if not report.persistent_halt:
        reasons.append("persistent_halt_absent")
    final_report = replace(report, stop_reasons=tuple(sorted(set(reasons))))
    return PreflightResult(
        exit_code=EXIT_SUCCESS if not final_report.stop_reasons else EXIT_PRECONDITION_FAILED,
        stop_reasons=final_report.stop_reasons,
        report=final_report,
    )


async def collect_preflight_report(
    session: AsyncSession,
    *,
    account_id: UUID,
    environment: str,
    artifact_hashes: Mapping[str, object],
    image_digest: str,
    projector_version: str,
) -> PreflightReport:
    """Collect parameterized, account-local observations without mutation."""
    rows = list(
        await session.scalars(
            select(EventLogRow)
            .where(
                EventLogRow.exchange_account_id == account_id,
                EventLogRow.deployment_environment == environment,
            )
            .order_by(EventLogRow.event_seq.asc())
        )
    )
    event_hash = canonical_event_hash(rows)
    open_uncertainty_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ExecutionUncertaintyRow)
            .where(
                ExecutionUncertaintyRow.exchange_account_id == account_id,
                ExecutionUncertaintyRow.deployment_environment == environment,
                ExecutionUncertaintyRow.state == "open",
            )
        )
        or 0
    )
    venue_snapshot_fence: int | None = None
    venue_snapshot_observed_at_ms: int | None = None
    venue_snapshot_complete = False
    from bfx_funding_bot.modules.execution.event_store.serialization import (
        deserialize_stored_event,
    )
    from bfx_funding_bot.modules.execution.events import VenueSnapshotObserved

    for row in reversed(rows):
        if row.event_type != "VENUE_SNAPSHOT_OBSERVED":
            continue
        try:
            snapshot = deserialize_stored_event(row)
        except (TypeError, ValueError):
            break
        if not isinstance(snapshot, VenueSnapshotObserved):
            break
        venue_snapshot_fence = row.event_seq
        venue_snapshot_observed_at_ms = snapshot.query_finished_at_ms
        venue_snapshot_complete = (
            snapshot.coverage.active_offers_complete
            and snapshot.coverage.active_credits_complete
            and snapshot.coverage.wallets_complete
        )
        break
    halted = await session.scalar(
        select(TradingHaltRow.halted)
        .where(
            TradingHaltRow.exchange_account_id == account_id,
            TradingHaltRow.deployment_environment == environment,
        )
        .order_by(TradingHaltRow.id.desc())
        .limit(1)
    )
    observed_projector_versions = tuple(
        sorted(
            {
                str(value)
                for value in await session.scalars(
                    select(ProjectionHeadRow.projector_version).where(
                        ProjectionHeadRow.exchange_account_id == account_id,
                        ProjectionHeadRow.deployment_environment == environment,
                    )
                )
            }
        )
    )
    schema_heads = tuple(
        sorted(str(value) for value in (await session.scalars(text("SELECT version_num FROM alembic_version"))))
    )
    return PreflightReport(
        exchange_account_id=str(account_id),
        deployment_environment=environment,
        migration_head=_migration_head(),
        schema_heads=schema_heads,
        backup_evidence_hash=str(artifact_hashes["backup_evidence_hash"]),
        isolated_restore_evidence_hash=str(artifact_hashes["isolated_restore_evidence_hash"]),
        event_count=len(rows),
        event_head=rows[-1].event_seq if rows else None,
        event_hash=event_hash,
        open_uncertainty_count=open_uncertainty_count,
        venue_snapshot_fence=venue_snapshot_fence,
        venue_snapshot_observed_at_ms=venue_snapshot_observed_at_ms,
        venue_snapshot_complete=venue_snapshot_complete,
        preflight_observed_at_ms=int(time.time() * 1000),
        backup_rpo_seconds=_optional_int(artifact_hashes.get("backup_rpo_seconds")),
        restore_rto_seconds=_optional_int(artifact_hashes.get("restore_rto_seconds")),
        config_digest=str(artifact_hashes["config_digest"]),
        image_digest=image_digest,
        projector_version=(
            projector_version if observed_projector_versions == (projector_version,) else None
        ),
        persistent_halt=halted is True,
        stop_reasons=(),
    )


def _migration_head() -> str | None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    return ",".join(sorted(heads)) if heads else None


def _load_evidence(path: Path) -> Halt2Evidence:
    with path.open(encoding="utf-8") as handle:
        raw: Any = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("evidence must be a JSON object")
    return Halt2Evidence.from_dict(raw)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_hashes(evidence: Halt2Evidence, *, config_artifact: Path) -> dict[str, object]:
    if Path(evidence.config_artifact_path).resolve() != config_artifact.resolve():
        raise ValueError("evidence config_artifact_path does not match --config-artifact")
    backup_measurement = _read_dr_measurement(
        Path(evidence.backup_evidence_path), key="rpo_seconds"
    )
    restore_measurement = _read_dr_measurement(
        Path(evidence.isolated_restore_evidence_path), key="rto_seconds"
    )
    return {
        "backup_evidence_hash": _file_digest(Path(evidence.backup_evidence_path)),
        "isolated_restore_evidence_hash": _file_digest(
            Path(evidence.isolated_restore_evidence_path)
        ),
        "config_digest": _file_digest(config_artifact),
        "backup_rpo_seconds": backup_measurement,
        "restore_rto_seconds": restore_measurement,
    }


def _read_dr_measurement(path: Path, *, key: str) -> int:
    """Read an explicit measured DR result; a file hash alone is not evidence."""
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{key}_measurement_unavailable") from exc
    if not isinstance(value, dict) or value.get("measured") is not True:
        raise ValueError(f"{key}_unmeasured")
    raw_seconds = value.get(key)
    if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, (int, str)):
        raise ValueError(f"{key}_measurement_invalid")
    try:
        seconds = int(raw_seconds)
    except ValueError as exc:
        raise ValueError(f"{key}_measurement_invalid") from exc
    if seconds < 0:
        raise ValueError(f"{key}_measurement_invalid")
    return seconds


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _max_snapshot_age_seconds(environ: Mapping[str, str]) -> int:
    raw = environ.get("BFX_HALT2_MAX_SNAPSHOT_AGE_SECONDS", "300").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("invalid_halt2_snapshot_age") from exc
    if value <= 0:
        raise ValueError("invalid_halt2_snapshot_age")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="preflight", choices=(
        "preflight", "assert-halt", "replay", "convert-pending", "quarantine", "verify", "release-report",
    ))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--projector-version", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--config-artifact", type=Path)
    parser.add_argument("--operator-id")
    parser.add_argument("--reason", default="halt2 preflight gate")
    return parser


async def _run(args: argparse.Namespace) -> PreflightResult:
    account_id = _validated_uuid(args.account_id)
    evidence = _load_evidence(args.evidence)
    if evidence.exchange_account_id != str(account_id):
        raise ValueError("evidence exchange_account_id does not match --account-id")
    if evidence.deployment_environment != args.environment:
        raise ValueError("evidence deployment_environment does not match --environment")
    if args.command in {"replay", "convert-pending", "quarantine", "verify", "release-report"}:
        raise ValueError(f"{args.command} is an explicit later-task operator command")
    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    try:
        if args.command == "assert-halt":
            if not args.operator_id:
                raise ValueError("assert-halt requires --operator-id")
            state = await HaltStateStore(
                factory, account_id=str(account_id), deployment_environment=args.environment
            ).set_halted(True, reason=args.reason, actor=args.operator_id)
            report = PreflightReport(
                exchange_account_id=str(account_id), deployment_environment=args.environment,
                migration_head=_migration_head(), schema_heads=(),
                backup_evidence_hash=evidence.backup_evidence_hash,
                isolated_restore_evidence_hash=evidence.isolated_restore_evidence_hash,
                event_count=0, event_head=None, event_hash="", open_uncertainty_count=0,
                venue_snapshot_fence=evidence.venue_snapshot_fence,
                venue_snapshot_observed_at_ms=evidence.venue_snapshot_observed_at_ms,
                venue_snapshot_complete=evidence.venue_snapshot_complete,
                preflight_observed_at_ms=evidence.preflight_observed_at_ms,
                backup_rpo_seconds=evidence.backup_rpo_seconds,
                restore_rto_seconds=evidence.restore_rto_seconds,
                config_digest=evidence.config_digest,
                image_digest=evidence.image_digest, persistent_halt=state.halted, stop_reasons=(),
                projector_version=args.projector_version,
            )
            return PreflightResult(EXIT_SUCCESS, (), report)
        if args.config_artifact is None:
            raise ValueError("preflight requires --config-artifact")
        artifact_hashes = _artifact_hashes(evidence, config_artifact=args.config_artifact)
        async with factory() as session:
            report = await collect_preflight_report(
                session,
                account_id=account_id,
                environment=args.environment,
                artifact_hashes=artifact_hashes,
                image_digest=args.image_digest,
                projector_version=args.projector_version,
            )
        return verify_preflight(report, evidence)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except ValueError as exc:
        print(json.dumps({"stop_reasons": [str(exc)]}, sort_keys=True))
        return EXIT_PRECONDITION_FAILED
    except Exception:
        print(json.dumps({"stop_reasons": ["verification_unavailable"]}, sort_keys=True))
        return EXIT_VERIFICATION_FAILED
    print(json.dumps(asdict(result.report), sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
