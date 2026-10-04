"""Run non-authorizing capital comparisons: python -m ...apps.capital_comparison.

Supply --dsn-file (a 0600 file holding the DSN; never pass credentials as arguments), --scope ACCOUNT_UUID:ENVIRONMENT (repeatable), --cells,
--run-id and --code-revision. Rehearsal takes --manifest; cutover instead takes
--cutover-manifest and --authorize-cutover-read. Output is JSONL on stdout.

The connection is a LOGIN that is a member of ``bfx_cutover_reader``; the command runs
``SET LOCAL ROLE`` to it and attests the reachable roles and privileges first
(``capital_comparison_guard``). A reverse inventory then proves the database holds no
authority state outside the listed scopes (``capital_comparison_inventory``); any
violation makes the run ``not_comparable`` (exit 1).
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Never, TextIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bfx_funding_bot.apps.capital_comparison_guard import (
    ConnectionPlan,
    GuardRejectedError,
    read_dsn,
    validate_connection,
    verify_connection,
)
from bfx_funding_bot.apps.capital_comparison_inventory import InventoryResult, reverse_inventory
from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS, load_cells_only
from bfx_funding_bot.modules.execution.capital_shadow_baseline import read_baseline
from bfx_funding_bot.modules.trading import Blocked, CapitalScope
from bfx_funding_bot.modules.trading_shadow import (
    CapitalComparator,
    ComparisonHeads,
    ShadowComparison,
    canonical_bytes,
)
from bfx_funding_bot.modules.trading_shadow.wiring import build_capital_comparator


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # argparse's normal error includes untrusted arguments (potentially a DSN).
        raise GuardRejectedError("invalid_arguments")


def parser() -> argparse.ArgumentParser:
    result = SafeParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--mode", required=True, choices=("rehearsal", "cutover"))
    result.add_argument("--dsn-file", type=Path, required=True)
    result.add_argument("--manifest", type=Path)
    result.add_argument("--cutover-manifest", type=Path)
    result.add_argument("--authorize-cutover-read", action="store_true")
    result.add_argument("--run-id", required=True)
    result.add_argument("--code-revision", required=True)
    result.add_argument("--cells", type=Path, required=True)
    result.add_argument("--scope", action="append", required=True)
    return result


def scopes_from_args(args: argparse.Namespace) -> tuple[CapitalScope, ...]:
    accounts = set()
    for item in args.scope:
        account, environment = item.split(":")
        if environment not in {"prod", "shadow", "ci"}:
            raise GuardRejectedError("invalid_scope")
        accounts.add((UUID(account), environment))
    cells = load_cells_only(args.cells)
    return tuple(sorted({
        CapitalScope(account, environment, cell.symbol, cell.cell_id)
        for account, environment in accounts for cell in cells
    }, key=lambda scope: (str(scope.account_id), scope.environment, scope.symbol, scope.cell_id)))


def emit(output: TextIO, value: object) -> None:
    output.write(canonical_bytes(value).decode() + "\n")
    output.flush()


def summarize(
    scopes: Sequence[CapitalScope], results: Sequence[ShadowComparison], *, inventory: InventoryResult
) -> dict[str, object]:
    counts = Counter(result.status for result in results)
    blocked = [r for r in results if r.status == "equal" and isinstance(r.candidate, Blocked)]
    inconclusive = not scopes or (len(blocked) == len(scopes) and all(
        isinstance(r.baseline, Blocked) and isinstance(r.candidate, Blocked)
        and r.baseline.reason == r.candidate.reason
        and (r.candidate.reason == "snapshot_stale" or "missing" in r.candidate.reason)
        for r in blocked
    ))
    complete = len(scopes) == len(set(scopes)) == len(results)
    passed = (
        complete and not inconclusive and counts["equal"] == len(scopes) and inventory.status == "ok"
    )
    return {
        "kind": "summary", "expected_scopes": len(scopes), "emitted_scopes": len(results),
        "coverage_complete": complete, "counts": dict(counts), "blocked_equal": len(blocked),
        "inconclusive": inconclusive, "baseline_evidence_complete": False,
        "inventory_status": inventory.status, "exit_code": 0 if passed else 1,
    }


async def compare_scopes(
    session: AsyncSession, *, scopes: Sequence[CapitalScope], comparator: CapitalComparator,
    plan: ConnectionPlan, code_revision: str, max_snapshot_age_ms: int, output: TextIO,
    inventory: InventoryResult,
) -> dict[str, object]:
    results = []
    provenance = {"code_revision": code_revision, "run_id": plan.run_id,
                  "now_ms": plan.now_ms, "mode": plan.mode}
    emit(output, {"kind": "inventory", "status": inventory.status,
                  "violations": list(inventory.violations), "provenance": provenance})
    # Enumeration and every comparison use this same caller-owned snapshot.
    for scope in scopes:
        head = (await session.execute(text(
            "SELECT revision, revision_id FROM public.capital_policy_heads "
            "WHERE exchange_account_id = :account AND deployment_environment = :environment "
            "AND symbol = :symbol"
        ), {"account": scope.account_id, "environment": scope.environment,
            "symbol": scope.symbol})).first()
        if head is None:
            result = ShadowComparison(
                "fold_comparison", "not_comparable", None, None, (), ("input_evidence_gap",),
                None, None, False, None, ComparisonHeads(), "policy_missing",
                (("table", "capital_policy_heads"), ("symbol", scope.symbol)),
            )
        else:
            # A comparator can catch SQL errors; rollback its savepoint on error so
            # other scopes retain the same outer RR snapshot rather than an aborted txn.
            savepoint = await session.begin_nested()
            try:
                result = await comparator(
                    session, scope=scope, now_ms=plan.now_ms,
                    max_snapshot_age_ms=max_snapshot_age_ms,
                )
            except Exception:
                result = ShadowComparison(
                    "fold_comparison", "error", None, None, (), (), None, None, False,
                    None, ComparisonHeads(), "comparator_error",
                )
            if result.status == "error":
                await savepoint.rollback()
            else:
                await savepoint.commit()
        results.append(result)
        payload = json.loads(canonical_bytes(result))
        payload.update({"scope": scope, "provenance": provenance,
                        "digests": {"candidate_input": result.candidate_input_digest,
                                    "baseline_input": None,
                                    "baseline_observation": result.baseline_observation_digest}})
        payload["heads"]["policy_revision"] = head[0] if head else None
        payload["heads"]["policy_revision_id"] = str(head[1]) if head else None
        emit(output, payload)
    summary = summarize(scopes, results, inventory=inventory)
    summary["provenance"] = provenance
    return summary


def connect(plan: ConnectionPlan) -> AsyncEngine:
    return create_async_engine(
        plan.url, echo=False, hide_parameters=True,
        connect_args={"timeout": 10, "server_settings": {"statement_timeout": "30000"}},
    )


async def run(
    argv: Sequence[str], *, output: TextIO, connector: Callable[[ConnectionPlan], AsyncEngine] = connect,
) -> int:
    try:
        args = parser().parse_args(argv)
        plan = validate_connection(
            mode=args.mode, dsn=read_dsn(args.dsn_file), manifest_path=args.manifest,
            cutover_manifest_path=args.cutover_manifest, run_id=args.run_id,
            authorize_cutover_read=args.authorize_cutover_read,
            wall_clock_ms=time.time_ns() // 1_000_000,
        )
        scopes = scopes_from_args(args)
        if not args.code_revision.strip():
            raise GuardRejectedError("invalid_comparison_parameters")
        comparator = build_capital_comparator(
            source_revision=args.code_revision, baseline_reader=read_baseline,
        )
        engine = connector(plan)
        try:
            async with (
                AsyncSession(engine, autoflush=False, expire_on_commit=False) as session,
                session.begin(),
            ):
                await session.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                await session.execute(text("SET LOCAL search_path TO public"))
                await verify_connection(session, plan)
                inventory = await reverse_inventory(
                    session, scopes=scopes, policy_without_cell=plan.policy_without_cell,
                )
                summary = await compare_scopes(
                    session, scopes=scopes, comparator=comparator, plan=plan,
                    code_revision=args.code_revision, max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS,
                    output=output, inventory=inventory,
                )
        finally:
            await engine.dispose()
        emit(output, summary)
        return int(str(summary["exit_code"]))
    except GuardRejectedError as exc:
        # Controlled reason codes (and catalog object names) only, never exception text.
        with suppress(Exception):
            emit(output, {"kind": "summary", "exit_code": 3, "reason": exc.reason,
                          "detail": list(exc.detail)})
        return 3
    except Exception:
        # Never serialize exceptions: SQLAlchemy/driver errors may contain credentials.
        with suppress(Exception):
            emit(output, {"kind": "summary", "exit_code": 3, "reason": "operational_failure"})
        return 3


def main() -> int:
    return asyncio.run(run(sys.argv[1:], output=sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
