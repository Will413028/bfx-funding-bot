"""Prefix-mode restore verification, run INSIDE the backend image on the isolated network.

restore_drill.py --prefix feeds this file to `python -` on the verifier
container's stdin (working directory /app), with only the ephemeral read-only
database URL of the restored cluster. It never sees production, R2 or any
application secret. It uses the image's own long-standing APIs, so the drill
does not depend on a CLI flag that older images lack:

1. the existing event-only replay (scripts.verify_projection_replay) for the
   scope -- identity, ordering, canonical event hash, projection parity;
2. the rolling prefix chain recomputed from the restored event_log with the
   writer's own `rolling_prefix_hash`: every event must have exactly one stored
   event_prefix_hashes link and every link must equal the recomputed value.

Prints one JSON line {"replay": {...}, "prefix": {"event_seq", "prefix_hash",
"chain_length"}}. The host then compares that head with production's link at the
same event_seq. Exit 3 with {"error": <bounded code>} on any verification failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

EXIT_VERIFICATION_FAILED = 3
CHAIN_ERRORS = ("prefix_chain_empty", "prefix_chain_mismatch", "prefix_chain_incomplete")


class ChainError(ValueError):
    pass


def verify_chain(rows: Iterable[Any], stored: Mapping[int, str]) -> dict[str, int | str]:
    """Recompute the scope's rolling prefix chain and require the stored links to match."""
    from bfx_funding_bot.modules.execution.event_store.canonical import (
        GENESIS_PREFIX_HASH,
        rolling_prefix_hash,
    )

    previous = GENESIS_PREFIX_HASH
    seen: list[int] = []
    for row in rows:
        previous = rolling_prefix_hash(previous, row)
        if stored.get(row.event_seq) != previous:
            raise ChainError("prefix_chain_mismatch")
        seen.append(row.event_seq)
    if not seen:
        raise ChainError("prefix_chain_empty")
    if set(stored) != set(seen):
        raise ChainError("prefix_chain_incomplete")
    return {"event_seq": seen[-1], "prefix_hash": previous, "chain_length": len(seen)}


async def _verify(account_id: UUID, environment: str, projector_version: str) -> dict[str, Any]:
    from sqlalchemy import select

    from bfx_funding_bot.core.db import make_engine, make_session_factory
    from bfx_funding_bot.core.settings import Settings
    from bfx_funding_bot.modules.execution.event_store.tables import (
        EventLogRow,
        EventPrefixHashRow,
    )
    from scripts.verify_projection_replay import render_replay_report, replay_one_account

    engine = make_engine(Settings())
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            report = render_replay_report(await replay_one_account(
                session, account_id=account_id, environment=environment,
                projector_version=projector_version,
            ))
            rows = list(await session.scalars(select(EventLogRow).where(
                EventLogRow.exchange_account_id == account_id,
                EventLogRow.deployment_environment == environment,
            ).order_by(EventLogRow.event_seq.asc())))
            stored = {
                link.event_seq: link.prefix_hash
                for link in await session.scalars(select(EventPrefixHashRow).where(
                    EventPrefixHashRow.exchange_account_id == account_id,
                    EventPrefixHashRow.deployment_environment == environment,
                ))
            }
    finally:
        await engine.dispose()
    return {"replay": report, "prefix": verify_chain(rows, stored)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--projector-version", required=True)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_verify(UUID(args.account_id), args.environment,
                                     args.projector_version))
    except ChainError as exc:
        print(json.dumps({"error": str(exc)}))
        return EXIT_VERIFICATION_FAILED
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"error": "replay_failed", "type": type(exc).__name__}))
        return EXIT_VERIFICATION_FAILED
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
