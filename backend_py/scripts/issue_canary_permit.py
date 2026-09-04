"""Issue one durable Halt 2 canary command permit.

Run from ``backend_py/``.  The command only appends a permit under the latest
persisted halt; it does not resume trading, start the daemon, or call Bitfinex.
The returned permit UUID must be supplied as ``BFX_CANARY_PERMIT_ID`` to the
same immutable canary process that will execute the one-shot command.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

if __package__ in {None, ""}:  # pragma: no cover - operator CLI path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bfx_funding_bot.core.db import make_engine, make_session_factory
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.execution.canary_permit import (
    CanaryPermitBlocked,
    CanaryPermitRepository,
    CanaryPermitScope,
)

EXIT_SUCCESS = 0
EXIT_PRECONDITION_FAILED = 2
EXIT_VERIFICATION_FAILED = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--amount-usdt", required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--now-ms", type=int)
    return parser


def _scope_from_args(args: argparse.Namespace) -> CanaryPermitScope:
    try:
        account_id = UUID(str(args.account_id))
        amount = Decimal(str(args.amount_usdt))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("account-id must be a UUID and amount-usdt must be numeric") from exc
    return CanaryPermitScope(
        account_id=account_id,
        environment=str(args.environment),
        symbol=str(args.symbol),
        cell=str(args.cell),
        strategy=str(args.strategy),
        amount_usdt=amount,
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    scope = _scope_from_args(args)
    now_ms = int(time.time() * 1000) if args.now_ms is None else args.now_ms
    if now_ms < 0:
        raise ValueError("now-ms must be non-negative")
    engine = make_engine(Settings())
    factory = make_session_factory(engine)
    try:
        permit = await CanaryPermitRepository(factory).issue(
            scope,
            operator_id=str(args.operator_id),
            now_ms=now_ms,
        )
        return {
            "permit_id": str(permit.permit_id),
            "halt_id": permit.halt_id,
            "account_id": str(permit.scope.account_id),
            "environment": permit.scope.environment,
            "symbol": permit.scope.symbol,
            "cell": permit.scope.cell,
            "strategy": permit.scope.strategy,
            "amount_usdt": str(permit.scope.amount_usdt),
            "operator_id": permit.operator_id,
            "state": permit.state,
            "issued_at_ms": permit.issued_at_ms,
        }
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = asyncio.run(_run(_parser().parse_args(argv)))
    except (CanaryPermitBlocked, ValueError) as exc:
        print(json.dumps({"stop_reasons": [str(exc)]}, sort_keys=True))
        return EXIT_PRECONDITION_FAILED
    except Exception:
        print(json.dumps({"stop_reasons": ["permit_unavailable"]}, sort_keys=True))
        return EXIT_VERIFICATION_FAILED
    print(json.dumps(result, sort_keys=True))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
