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
from collections.abc import Sequence
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

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

try:  # ``python scripts/...`` has scripts/ rather than its parent on sys.path.
    from scripts.halt2_cutover import Halt2Evidence, _artifact_hashes, _load_evidence
    from scripts.verify_projection_replay import replay_event_log
except ModuleNotFoundError:  # pragma: no cover - exercised by the operator CLI.
    from halt2_cutover import Halt2Evidence, _artifact_hashes, _load_evidence
    from verify_projection_replay import replay_event_log

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
    _require_halt2_artifacts(halt2_evidence, args.config_artifact)
    evidence = load_canary_evidence(args.evidence)
    engine = make_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
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
                now_ms=now_ms_utc(),
            )
        assert_canary_startup(
            profile=profile,
            evidence=evidence,
            readiness=readiness,
            configured_cells=tuple(
                (cell.strategy.value, cell.symbol, cell.cell_id) for cell in config.cells
            ),
            configured_caps=safety_config.hard_guards.allocation_cap.caps,
            allocation_cap_usdt=allocation_cap_usdt,
        )
        return evidence
    finally:
        await engine.dispose()


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
