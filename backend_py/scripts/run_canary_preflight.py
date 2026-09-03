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
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.safety.config import load_safety_config
from bfx_funding_bot.modules.marketfeed.config import load_config
from bfx_funding_bot.modules.marketfeed.daemon import (
    CanaryEvidence,
    CanaryProfile,
    CanaryStartupBlocked,
    assert_canary_guard_invariant,
    assert_canary_startup,
    collect_canary_readiness,
    load_canary_evidence,
)
from bfx_funding_bot.modules.marketfeed.scheduler import now_ms_utc

# Direct ``python scripts/...`` execution otherwise exposes scripts/ rather
# than its parent package. This only resolves local source imports; it does not
# affect evidence inputs or runtime configuration.
if __package__ in {None, ""}:  # pragma: no cover - operator CLI path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.halt2_cutover import (
    Halt2Evidence,
    _artifact_hashes,
    _load_evidence,
    collect_preflight_report,
    verify_preflight,
)
from scripts.verify_projection_replay import replay_event_log

EXIT_SUCCESS = 0
EXIT_PRECONDITION_FAILED = 2
EXIT_VERIFICATION_FAILED = 3


def _projection_hash(rows: Sequence[EventLogRow], *, profile: CanaryProfile) -> str:
    """Reuse the event-only replay implementation; runtime projections stay diagnostic."""
    replay = replay_event_log(
        rows,
        account_id=profile.account_id,
        environment=profile.environment,
    )
    return hashlib.sha256(
        json.dumps(replay.content_hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    profile = CanaryProfile.from_environ(args.environ)
    config = load_config(cells_yaml_path=args.cells)
    safety_config_path = Path(args.environ.get("BFX_SAFETY_CONFIG", "configs/safety.yaml"))
    safety_config = load_safety_config(safety_config_path)
    assert_canary_guard_invariant(config.phase, safety_config)
    try:
        allocation_cap_usdt = Decimal(args.environ["BFX_ALLOCATION_CAP_USDT"])
    except (ArithmeticError, KeyError, ValueError) as exc:
        raise CanaryStartupBlocked("invalid_canary_allocation_cap") from exc
    if config.deployment_environment.value != profile.environment:
        raise CanaryStartupBlocked("canary_config_environment_mismatch")
    halt2_evidence = _load_evidence(args.halt2_evidence)
    if (
        halt2_evidence.exchange_account_id != str(profile.account_id)
        or halt2_evidence.deployment_environment != profile.environment
    ):
        raise CanaryStartupBlocked("halt2_identity_or_environment_mismatch")
    evidence = load_canary_evidence(args.evidence)
    engine = make_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            await verify_canary_preflight(
                session=session,
                profile=profile,
                halt2_evidence=halt2_evidence,
                config_artifact=args.config_artifact,
                image_digest=args.environ.get("BFX_EXPECTED_IMAGE_DIGEST", ""),
                projector_version=args.environ.get("BFX_PROJECTOR_VERSION", ""),
                environ=args.environ,
                evidence=evidence,
                configured_cells=tuple(
                    (cell.strategy.value, cell.symbol, cell.cell_id) for cell in config.cells
                ),
                configured_caps=safety_config.hard_guards.allocation_cap.caps,
                allocation_cap_usdt=allocation_cap_usdt,
                now_ms=now_ms_utc(),
            )
        return evidence
    finally:
        await engine.dispose()


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
) -> None:
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
        assert_canary_startup(
            profile=profile,
            evidence=None,
            readiness=await collect_canary_readiness(
                session,
                account_id=profile.account_id,
                environment=profile.environment,
                now_ms=now_ms,
            ),
            configured_cells=configured_cells,
            configured_caps=configured_caps,
            allocation_cap_usdt=allocation_cap_usdt,
        )
        return
    rows = list(
        await session.scalars(
            select(EventLogRow)
            .where(
                EventLogRow.exchange_account_id == profile.account_id,
                EventLogRow.deployment_environment == profile.environment,
            )
            .order_by(EventLogRow.event_seq.asc())
        )
    )
    if evidence.projection_hash != _projection_hash(rows, profile=profile):
        raise CanaryStartupBlocked("projection_hash_mismatch")
    readiness = await collect_canary_readiness(
        session,
        account_id=profile.account_id,
        environment=profile.environment,
        now_ms=now_ms,
    )
    assert_canary_startup(
        profile=profile,
        evidence=evidence,
        readiness=readiness,
        configured_cells=configured_cells,
        configured_caps=configured_caps,
        allocation_cap_usdt=allocation_cap_usdt,
    )


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
