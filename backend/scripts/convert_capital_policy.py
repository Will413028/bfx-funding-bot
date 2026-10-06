"""Dry-run -> explicit digest-confirmed capital conversion; never resume.

uv run python scripts/convert_capital_policy.py --exchange-account-id UUID \
  --environment REALM --legacy-source legacy.json --max-snapshot-age-ms 60000
Repeat with --apply-digest DIGEST only after reviewing the complete dry-run.

legacy.json is an explicit non-secret export: schema_version=1, caps, buffers,
default_cap, default_buffer, env_fallback_cap, env_fallback_buffer and
max_cell_fraction. Amounts are decimal strings; env fallbacks may be null. These
inputs exist only for migration comparison, never runtime capital fallback.
DATABASE_URL must be supplied explicitly; this command does not load .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from bfx_funding_bot.core.db import make_async_engine_from_url, make_session_factory
from bfx_funding_bot.modules.accounts.capital_conversion import convert_capital_policy
from bfx_funding_bot.modules.execution.capital_policy_read import CapitalBlockedError
from bfx_funding_bot.modules.execution.capital_repository import CapitalRepository
from bfx_funding_bot.modules.execution.legacy_ports import LegacyScopeLock


async def run(args: argparse.Namespace) -> dict[str, Any]:
    legacy = json.loads(args.legacy_source.read_text(), parse_float=Decimal)
    if not isinstance(legacy, dict):
        raise CapitalBlockedError("invalid_legacy_document")
    engine = make_async_engine_from_url(os.environ["DATABASE_URL"])
    try:
        repo = CapitalRepository(account_id=args.exchange_account_id, environment=args.environment,
                                 max_snapshot_age_ms=args.max_snapshot_age_ms)
        async with make_session_factory(engine)() as session:
            report = await convert_capital_policy(session, repository=repo,
                scope_lock=LegacyScopeLock(repo), legacy=legacy,
                now_ms=time.time_ns() // 1_000_000, apply_digest=args.apply_digest)
            if args.apply_digest:
                await session.commit()
            else:
                await session.rollback()
            return report
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange-account-id", type=UUID, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--legacy-source", type=Path, required=True)
    parser.add_argument("--max-snapshot-age-ms", type=int, required=True)
    parser.add_argument("--apply-digest")
    args = parser.parse_args()
    logging.disable(sys.maxsize)
    try:
        report = asyncio.run(run(args))
        print(json.dumps(report, sort_keys=True, indent=2))
        return 2 if report["status"] == "invalid" else 0
    except CapitalBlockedError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    except Exception:
        # Never expose a connection URL, DB bind values or credentials.
        print('{"status":"blocked","reason":"conversion_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
