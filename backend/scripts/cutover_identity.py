"""Operator CLI for the Halt 1 ExchangeAccount identity cutover.

The command is intentionally separate from Alembic: schema revisions never
touch encrypted plaintext or infer a realm mapping from process environment.
Run from ``backend/`` with ``DATABASE_URL`` and ``BFX_VAULT_KEK`` configured.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from bfx_funding_bot.core.crypto import load_kek
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.modules.accounts.identity_cutover import IdentityCutover, IdentityManifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply the validated mapping")
    mode.add_argument("--verify", action="store_true", help="verify an already-applied mapping")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="execute the application movement inside a rollback-only savepoint",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="immutable preflight JSON report consumed by --verify",
    )
    parser.add_argument(
        "--verify-permissions",
        action="store_true",
        help="perform live Bitfinex funding-write/withdraw-off permission checks",
    )
    return parser


def _load_manifest(path: Path) -> IdentityManifest:
    with path.open(encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("identity manifest JSON must be an object")
    return IdentityManifest.from_dict(payload)


def _load_evidence(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        payload: Any = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("cutover evidence JSON must be an object")
    return payload


async def _run(args: argparse.Namespace) -> None:
    if args.dry_run and not args.apply:
        raise SystemExit("--dry-run requires --apply")
    if args.evidence is not None and not args.verify:
        raise SystemExit("--evidence requires --verify")
    if args.verify and args.evidence is None:
        raise SystemExit("--verify requires --evidence")
    if args.verify and not args.verify_permissions:
        raise SystemExit("--verify requires --verify-permissions")
    manifest = _load_manifest(args.manifest)
    cutover = IdentityCutover(kek=load_kek())
    settings = Settings()
    engine = make_engine(settings)
    factory = make_session_factory(engine)
    http: httpx.AsyncClient | None = None
    try:
        permissions_client = None
        expected_report = None
        if args.verify:
            expected_report = _load_evidence(args.evidence)
            http = httpx.AsyncClient()
            permissions_client = BitfinexAuthREST(http=http)
        async with session_scope(factory) as session:
            if args.verify:
                report = await cutover.verify(
                    session,
                    manifest,
                    expected_report=expected_report,
                    permissions_client=permissions_client,
                )
            elif args.apply:
                report = await cutover.apply(session, manifest, dry_run=args.dry_run)
            else:
                report = await cutover.preflight(session, manifest)
            print(json.dumps(asdict(report), sort_keys=True))
    finally:
        if http is not None:
            await http.aclose()
        await engine.dispose()


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
