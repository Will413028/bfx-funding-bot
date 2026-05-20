#!/usr/bin/env python
"""G1 deployment-gate smoke check.

Design basis: phase4.1-paper-shadow-infra-design.md Section "G1 Smoke Check Script"

Usage:
    uv run python scripts/g1_smoke_check.py [--hours 1] [--json] [--only C1,C2,C3]

Exit codes:
    0  all checks passed (G1 OK)
    1  one or more checks failed
    2  unable to query Axiom (auth / network)
    3  config error (missing EDA range / cells.yaml mismatch)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from bfx_funding_bot.smoke.eda_ranges import EDA_RANGES


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False


class AxiomQueryClient:
    def __init__(
        self, api_key: str, dataset: str, base_url: str = "https://api.axiom.co",
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=30.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self.dataset = dataset

    @staticmethod
    def _tabular_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        tables = payload.get("tables") or []
        if not tables:
            return []
        t = tables[0]
        fields = [f["name"] for f in t.get("fields", [])]
        cols = t.get("columns") or []
        if not fields or not cols:
            return []
        n_rows = len(cols[0])
        return [
            {fields[i]: cols[i][r] for i in range(len(fields))}
            for r in range(n_rows)
        ]

    async def query_apl(self, apl: str) -> list[dict[str, Any]]:
        resp = await self._http.post(
            "/v1/datasets/_apl?format=tabular",
            json={"apl": apl},
        )
        if resp.status_code in (401, 403):
            raise SystemExit(2)
        if resp.status_code >= 400:
            print(f"[debug] APL: {apl!r}", flush=True)
            print(f"[debug] response: {resp.text!r}", flush=True)
        resp.raise_for_status()
        return self._tabular_to_rows(resp.json())

    async def aclose(self) -> None:
        await self._http.aclose()


def build_apl_query_c1(phase: str, dataset: str, hours: int) -> str:
    return f"""
['{dataset}']
| where ['event_type'] == 'signal' and phase == '{phase}' and _time > ago({hours}h)
| summarize count() by bin(_time, 5m)
| order by _time asc
""".strip()


def build_apl_query_c2(phase: str, dataset: str, hours: int) -> str:
    return f"""
['{dataset}']
| where ['event_type'] == 'signal' and phase == '{phase}' and _time > ago({hours}h)
| summarize count() by strategy, cell
""".strip()


def build_apl_query_c3(
    phase: str,
    dataset: str,
    hours: int,
    strategy: str,
    cell: str,
    lo: float,
    hi: float,
) -> str:
    return f"""
['{dataset}']
| where ['event_type'] == 'signal' and phase == '{phase}' and _time > ago({hours}h)
  and strategy == '{strategy}' and cell == '{cell}'
  and (toreal(['payload.signal_score']) < {lo} or toreal(['payload.signal_score']) > {hi})
| count
""".strip()


def build_apl_query_c5(phase: str, dataset: str, hours: int) -> str:
    return f"""
['{dataset}']
| where ['event_type'] == 'health_check' and ['level'] in ('error', 'critical')
  and _time > ago({hours}h) and phase == '{phase}'
| count
""".strip()


def build_apl_query_c6(phase: str, dataset: str, hours: int) -> str:
    return f"""
['{dataset}']
| where ['event_type'] == 'signal' and ['level'] == 'warn' and _time > ago({hours}h)
  and phase == '{phase}' and isnotnull(['payload.divergence_detail.diff_fields'])
| count
""".strip()


async def run_c1_continuity(
    *,
    client: AxiomQueryClient,
    phase: str,
    hours: int,
    cells: list[dict[str, Any]] | None = None,  # consumed in Task 3
    now_fn: Callable[[], datetime] | None = None,  # consumed in Task 3
) -> CheckResult:
    """C1 continuity check.

    NOTE: This is the legacy bucket-based implementation. Task 3 replaces it with
    per-cell max-gap + recency. `cells` and `now_fn` params accepted but unused here.
    """
    apl = build_apl_query_c1(phase, client.dataset, hours)
    buckets = await client.query_apl(apl)
    expected = hours * 12
    empty = [b for b in buckets if b.get("count_", 0) == 0]
    if len(buckets) >= expected and not empty:
        return CheckResult(
            "C1: continuity", True,
            detail=f"{len(buckets)}/{expected} 5min buckets non-empty",
        )
    return CheckResult(
        "C1: continuity", False,
        detail=f"got {len(buckets)} buckets, {len(empty)} empty (expected {expected} all non-empty)",
    )


async def run_c2_emit_completeness(
    *, client: AxiomQueryClient, phase: str, hours: int, cells: list[dict],  # type: ignore[type-arg]
) -> CheckResult:
    apl = build_apl_query_c2(phase, client.dataset, hours)
    rows = await client.query_apl(apl)
    actual = {(r.get("strategy"), r.get("cell")): r.get("count_", 0) for r in rows}

    failures: list[str] = []
    total_expected = 0
    total_actual = 0
    for c in cells:
        strat = c["strategy"]
        cell_id = f"{c['symbol']}_{c['period_agg']}"
        tf = c.get("timeframe", "1h")
        per_hour = {"15m": 4, "30m": 2, "1h": 1}[tf]
        expected = per_hour * hours
        got = actual.get((strat, cell_id), 0)
        total_expected += expected
        total_actual += got
        if got != expected:
            failures.append(f"{strat}:{cell_id} expected={expected} actual={got}")
    if not failures:
        return CheckResult(
            "C2: emit completeness", True,
            detail=f"expected={total_expected} actual={total_actual} (across {len(cells)} cells)",
        )
    return CheckResult("C2: emit completeness", False, detail="; ".join(failures))


async def run_c3_range_conformance(
    *, client: AxiomQueryClient, phase: str, hours: int, cells: list[dict],  # type: ignore[type-arg]
) -> CheckResult:
    failures: list[str] = []
    total_checked = 0
    for c in cells:
        strat = c["strategy"]
        cell_id = f"{c['symbol']}_{c['period_agg']}"
        rng = EDA_RANGES.get((strat, cell_id))
        if rng is None:
            return CheckResult(
                "C3: range conformance", False,
                detail=f"no EDA range for ({strat}, {cell_id})",
            )
        apl = build_apl_query_c3(
            phase, client.dataset, hours, strat, cell_id,
            rng["signal_score_min"], rng["signal_score_max"],
        )
        rows = await client.query_apl(apl)
        n_outliers = int(rows[0].get("count_", 0)) if rows else 0
        total_checked += 1
        if n_outliers > 0:
            failures.append(f"{strat}:{cell_id} {n_outliers} outliers")
    if not failures:
        return CheckResult(
            "C3: range conformance", True,
            detail=f"0 outliers across {total_checked} cell x strategy pairs",
        )
    return CheckResult("C3: range conformance", False, detail="; ".join(failures))


async def run_c5_zero_error_health(
    *, client: AxiomQueryClient, phase: str, hours: int,
) -> CheckResult:
    apl = build_apl_query_c5(phase, client.dataset, hours)
    rows = await client.query_apl(apl)
    count = int(rows[0].get("count_", 0)) if rows else 0
    if count == 0:
        return CheckResult("C5: zero error/critical health_check", True, detail="0 events")
    return CheckResult("C5: zero error/critical health_check", False, detail=f"{count} events")


async def run_c6_zero_divergence(
    *, client: AxiomQueryClient, phase: str, hours: int,
) -> CheckResult:
    apl = build_apl_query_c6(phase, client.dataset, hours)
    rows = await client.query_apl(apl)
    count = int(rows[0].get("count_", 0)) if rows else 0
    if count == 0:
        return CheckResult("C6: zero divergence warn", True, detail="0 events")
    return CheckResult(
        "C6: zero divergence warn", False,
        detail=f"{count} divergence events -- pipeline correctness violated",
    )


async def run_smoke_async(
    *,
    phase: str,
    hours: int,
    cells: list[dict[str, Any]],
    only: set[str] | None = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    json_output: bool = False,
) -> int:
    """Run G1 smoke checks. Returns exit code (0=pass, 1=fail, 2=auth, 3=config).

    Args:
        phase: BFX phase (paper / shadow / canary).
        hours: lookback window in hours.
        cells: cell config dicts (each with strategy, symbol, period_agg, timeframe).
        only: subset of check names to run (None = all).
        now_fn: injectable clock for C1 recency check (UTC aware).
        json_output: if True, print results as JSON (daemon default: False).
    """
    axiom_api_key = os.environ.get("AXIOM_API_KEY", "")
    axiom_dataset = os.environ.get("AXIOM_DATASET", "")
    if not axiom_api_key or not axiom_dataset:
        print("ERROR: AXIOM_API_KEY and AXIOM_DATASET required", file=sys.stderr)
        return 3

    client = AxiomQueryClient(api_key=axiom_api_key, dataset=axiom_dataset)
    try:
        results: list[CheckResult] = []
        if only is None or "C1" in only:
            results.append(await run_c1_continuity(
                client=client, phase=phase, hours=hours, cells=cells, now_fn=now_fn,
            ))
        if only is None or "C2" in only:
            results.append(await run_c2_emit_completeness(
                client=client, phase=phase, hours=hours, cells=cells,
            ))
        if only is None or "C3" in only:
            results.append(await run_c3_range_conformance(
                client=client, phase=phase, hours=hours, cells=cells,
            ))
        if only is None or "C5" in only:
            results.append(await run_c5_zero_error_health(
                client=client, phase=phase, hours=hours,
            ))
        if only is None or "C6" in only:
            results.append(await run_c6_zero_divergence(
                client=client, phase=phase, hours=hours,
            ))
        results.append(CheckResult(
            "C4: dashboard liveness", True, skipped=True,
            detail="(4.1 V1 -- dashboard widget belongs to 4.4)",
        ))

        return _report(results, json_output=json_output)
    except SystemExit as e:
        return int(e.code) if e.code is not None else 2
    finally:
        await client.aclose()


async def main_async(args: argparse.Namespace) -> int:
    """CLI entrypoint: load yaml, parse env, call run_smoke_async."""
    phase = os.environ.get("BFX_PHASE", "paper")
    cells_yaml = Path(
        args.cells_yaml or Path(__file__).parents[3] / "configs" / "cells.yaml",
    )
    cells_data = yaml.safe_load(cells_yaml.read_text())
    cells = cells_data["cells"]
    only = set((args.only or "").split(",")) if args.only else None
    return await run_smoke_async(
        phase=phase, hours=args.hours, cells=cells, only=only,
        json_output=args.json,
    )


def _report(results: list[CheckResult], *, json_output: bool = False) -> int:
    failed = [r for r in results if not r.passed and not r.skipped]
    if json_output:
        print(json.dumps({
            "checks": [r.__dict__ for r in results],
            "passed": len(failed) == 0,
        }, indent=2))
    else:
        print("G1 Smoke Check -- bfx-funding-bot 4.1")
        print("=" * 50)
        for r in results:
            status = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
            print(f"[ {r.name:<40s} ] {status}  {r.detail}")
        print("=" * 50)
        if failed:
            print(f"Result: FAIL ({len(failed)} checks)")
            print("Action: G1 failed -- run systematic-debugging skill for root cause")
        else:
            print("Result: PASS")
            print("Action: ready for shadow phase -- redeploy with BFX_PHASE=shadow")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", type=str, default=None)
    ap.add_argument("--cells-yaml", type=str, default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
