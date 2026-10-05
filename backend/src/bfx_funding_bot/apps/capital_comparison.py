"""Run non-authorizing capital comparisons: python -m ...apps.capital_comparison.

Supply --dsn-file (a 0600 file holding the DSN; never pass credentials as arguments), --scope ACCOUNT_UUID:ENVIRONMENT (repeatable), --cells,
--run-id and --code-revision. Rehearsal takes --manifest; cutover instead takes
--cutover-manifest, --authorize-cutover-read, --observation (the cutover runner's REST
observations, ``execution.capital_observed_baseline``) and --seed-evidence (the seed
command's JSONL). Output is JSONL on stdout.

The connection is a LOGIN that is a member of ``bfx_cutover_reader``; the command runs
``SET LOCAL ROLE`` to it and attests the reachable roles and privileges first
(``capital_comparison_guard``). A reverse inventory then proves the database holds no
authority state outside the listed scopes (``capital_comparison_inventory``); any
violation makes the run ``not_comparable`` (exit 1).

Arms, all in one REPEATABLE READ READ ONLY transaction:

* rehearsal: ``fold_comparison`` (legacy stored baseline vs the S0 fold);
* cutover: ``ledger_reader`` (legacy acceptance of the runner observation, read only, vs the
  ledger capital reader, both at the manifest's as-of ``now_ms``,
  ``capital_comparison_ledger``) and ``closure`` (the seed is the legacy closure,
  ``capital_comparison_closure``). The as-of must equal the observation's completion
  instant, and the wall clock from the earliest observation's query start to the end of the
  command must stay within ``CAPITAL_MAX_SNAPSHOT_AGE_MS`` (Q6). Exit 0 only if every arm
  passes, the inventory is clean and the window holds.
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Never, TextIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from bfx_funding_bot.apps.capital_comparison_closure import (
    EvidenceRejectedError,
    SeedEvidence,
    closure_json,
    parse_seed_evidence,
    summarize_closure,
    verify_closure,
)
from bfx_funding_bot.apps.capital_comparison_guard import (
    ConnectionPlan,
    GuardRejectedError,
    read_dsn,
    validate_connection,
    verify_connection,
)
from bfx_funding_bot.apps.capital_comparison_inventory import InventoryResult, reverse_inventory
from bfx_funding_bot.apps.capital_comparison_ledger import (
    ARM,
    ArmResult,
    arm_json,
    compare_ledger_arm,
    summarize_arm,
)
from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS, load_cells_only
from bfx_funding_bot.modules.execution.capital_observed_baseline import (
    CutoverObservation,
    ObservationRejectedError,
    ObservedScope,
    evaluate_scope,
    observed_as_of_ms,
    parse_observations,
    window_start_ms,
)
from bfx_funding_bot.modules.execution.capital_shadow_baseline import read_baseline
from bfx_funding_bot.modules.ledger import (
    LedgerCapitalReader,
    LedgerManagedOffers,
    LedgerUncertainties,
)
from bfx_funding_bot.modules.ledger.wiring import (
    build_ledger_capital_reader,
    build_ledger_managed_offers,
    build_ledger_uncertainties,
)
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
    result.add_argument("--observation", type=Path)
    result.add_argument("--seed-evidence", type=Path)
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


@dataclass(frozen=True, slots=True)
class CutoverInputs:
    observations: tuple[CutoverObservation, ...]
    seeds: Mapping[tuple[UUID, str], SeedEvidence]
    window_start_ms: int


def cutover_inputs(
    args: argparse.Namespace, plan: ConnectionPlan, scopes: Sequence[CapitalScope],
) -> CutoverInputs:
    """The runner observation and the seed evidence, checked against the plan and scopes."""
    if args.observation is None or args.seed_evidence is None:
        raise GuardRejectedError("cutover_inputs_required")
    try:
        observations = parse_observations(json.loads(args.observation.read_text()))
        seeds = parse_seed_evidence(args.seed_evidence.read_text().splitlines())
    except (ObservationRejectedError, EvidenceRejectedError) as exc:
        raise GuardRejectedError(exc.reason) from None
    except (OSError, ValueError):
        raise GuardRejectedError("cutover_inputs_unreadable") from None
    listed = {(scope.account_id, scope.environment) for scope in scopes}
    if {(o.account_id, o.environment) for o in observations} != listed:
        raise GuardRejectedError("observation_scope_mismatch")
    if plan.now_ms != observed_as_of_ms(observations):
        # The as-of is the observation's completion instant, not an operator's choice.
        raise GuardRejectedError("as_of_mismatch")
    return CutoverInputs(observations, seeds, window_start_ms(observations))


async def compare_cutover(
    session: AsyncSession, *, scopes: Sequence[CapitalScope], inputs: CutoverInputs,
    plan: ConnectionPlan, code_revision: str, max_snapshot_age_ms: int, output: TextIO,
    inventory: InventoryResult, ledger_reader: LedgerCapitalReader,
    managed_offers: LedgerManagedOffers, uncertainties: LedgerUncertainties,
) -> dict[str, object]:
    """The ``ledger_reader`` and ``closure`` arms in the caller's one read-only snapshot."""
    provenance = {"code_revision": code_revision, "run_id": plan.run_id,
                  "now_ms": plan.now_ms, "mode": plan.mode,
                  "window_start_ms": inputs.window_start_ms}
    emit(output, {"kind": "inventory", "status": inventory.status,
                  "violations": list(inventory.violations), "provenance": provenance})
    evaluated: dict[tuple[UUID, str], ObservedScope | None] = {}
    for observation in inputs.observations:
        savepoint = await session.begin_nested()
        try:
            evaluated[(observation.account_id, observation.environment)] = await evaluate_scope(
                session, observation, now_ms=plan.now_ms, max_snapshot_age_ms=max_snapshot_age_ms,
            )
            await savepoint.commit()
        except Exception:
            await savepoint.rollback()
            evaluated[(observation.account_id, observation.environment)] = None
    results: list[ArmResult] = []
    for scope in scopes:
        scoped = evaluated.get((scope.account_id, scope.environment))
        if scoped is None:
            result = ArmResult(ARM, scope, "error", reason="legacy_evaluation_error")
        else:
            savepoint = await session.begin_nested()
            result = await compare_ledger_arm(
                session, scope=scope, evaluated=scoped, ledger_reader=ledger_reader,
                now_ms=plan.now_ms, max_snapshot_age_ms=max_snapshot_age_ms,
            )
            if result.status == "error":
                await savepoint.rollback()
            else:
                await savepoint.commit()
        results.append(result)
        emit(output, {**arm_json(result), "provenance": provenance})
    savepoint = await session.begin_nested()
    try:
        checks = await verify_closure(
            session, scopes=scopes, seeds=inputs.seeds, managed_offers=managed_offers,
            uncertainties=uncertainties,
        )
        await savepoint.commit()
        closure = summarize_closure(scopes, checks)
    except Exception as exc:
        await savepoint.rollback()
        checks = ()
        closure = {"checks": 0, "violations": 0, "coverage_complete": False, "passed": False,
                   "error": type(exc).__name__}
    for check in checks:
        emit(output, {**closure_json(check), "provenance": provenance})
    return {
        "kind": "summary", "mode": plan.mode, "inventory_status": inventory.status,
        "arms": {ARM: summarize_arm(scopes, results), "closure": closure},
        "provenance": provenance,
    }


def finish_cutover(summary: dict[str, object], *, window_start: int, now: int, limit_ms: int) -> dict[str, object]:
    """Q6's wall-clock window (earliest observation query start -> command end) and the exit code."""
    elapsed = now - window_start
    window_ok = 0 <= elapsed <= limit_ms
    arms = summary["arms"]
    assert isinstance(arms, dict)
    passed = (
        window_ok and summary["inventory_status"] == "ok"
        and all(arm["passed"] is True for arm in arms.values())
    )
    return {**summary, "window": {"start_ms": window_start, "end_ms": now, "elapsed_ms": elapsed,
                                  "limit_ms": limit_ms, "ok": window_ok},
            "exit_code": 0 if passed else 1}


def connect(plan: ConnectionPlan) -> AsyncEngine:
    return create_async_engine(
        plan.url, echo=False, hide_parameters=True,
        connect_args={"timeout": 10, "server_settings": {"statement_timeout": "30000"}},
    )


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


async def run(
    argv: Sequence[str], *, output: TextIO, connector: Callable[[ConnectionPlan], AsyncEngine] = connect,
    clock: Callable[[], int] = wall_clock_ms,
) -> int:
    try:
        args = parser().parse_args(argv)
        plan = validate_connection(
            mode=args.mode, dsn=read_dsn(args.dsn_file), manifest_path=args.manifest,
            cutover_manifest_path=args.cutover_manifest, run_id=args.run_id,
            authorize_cutover_read=args.authorize_cutover_read,
            wall_clock_ms=clock(),
        )
        scopes = scopes_from_args(args)
        if not args.code_revision.strip():
            raise GuardRejectedError("invalid_comparison_parameters")
        if plan.mode == "cutover":
            return await run_cutover(args, plan, scopes, output=output, connector=connector,
                                     clock=clock)
        if args.observation is not None or args.seed_evidence is not None:
            raise GuardRejectedError("cutover_inputs_in_rehearsal")
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


async def run_cutover(
    args: argparse.Namespace, plan: ConnectionPlan, scopes: Sequence[CapitalScope], *,
    output: TextIO, connector: Callable[[ConnectionPlan], AsyncEngine], clock: Callable[[], int],
) -> int:
    inputs = cutover_inputs(args, plan, scopes)
    engine = connector(plan)
    try:
        async with (
            AsyncSession(engine, autoflush=False, expire_on_commit=False) as session,
            session.begin(),
        ):
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            await session.execute(text("SET LOCAL search_path TO public"))
            await verify_connection(session, plan)
            inventory = await reverse_inventory(
                session, scopes=scopes, policy_without_cell=plan.policy_without_cell,
            )
            summary = await compare_cutover(
                session, scopes=scopes, inputs=inputs, plan=plan,
                code_revision=args.code_revision, max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS,
                output=output, inventory=inventory, ledger_reader=build_ledger_capital_reader(),
                managed_offers=build_ledger_managed_offers(),
                uncertainties=build_ledger_uncertainties(),
            )
    finally:
        await engine.dispose()
    summary = finish_cutover(summary, window_start=inputs.window_start_ms, now=clock(),
                             limit_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS)
    emit(output, summary)
    return int(str(summary["exit_code"]))


def main() -> int:
    return asyncio.run(run(sys.argv[1:], output=sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
