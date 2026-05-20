#!/usr/bin/env python
"""Phase 4.3 G2 calibration audit — stale vs fresh signal subset analysis.

Queries Axiom for shadow window stale signals (is_stale=True) + fresh signals,
joins with decision events to compute win-rate per cell x strategy pair.
Applies Phase 3b qualification gate to stale subset.

Design basis: docs/superpowers/specs/2026-05-20-phase4.3-locf-design.md
  Section "G2 Calibration Audit Plan" + APL templates F1/F2/F3.

Usage:
    AXIOM_API_KEY=... AXIOM_DATASET=bfx-funding-bot \\
        uv run python scripts/g2_audit_locf.py \\
        --window-start 2026-05-20T00:00:00Z \\
        --window-end   2026-06-03T00:00:00Z \\
        --output docs/research/$(date -u +%Y-%m-%d)-phase4.3-locf-g2-audit-log.md

Exit codes:
    0  audit complete (report written)
    2  unable to query Axiom (auth / network / missing env)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Audit configuration
# ---------------------------------------------------------------------------

AUDIT_PAIRS: list[tuple[str, str]] = [
    ("fUSD_p30", "rate_percentile"),
    ("fUSD_p30", "mean_reversion"),
    ("fUST_p30", "rate_percentile"),
    # fUST_p30 x mean_reversion excluded — not in cells.yaml (Phase 3b carry-over)
]

MIN_SAMPLE_SIZE: int = 30
WIN_RATE_FLOOR: float = 0.60
MARGIN_FLOOR: float = 0.05
YELLOW_WIN_RATE_THRESHOLD: float = 0.55

# Decision join tolerance: stale signal + its matching decision within this many seconds
DECISION_JOIN_TOLERANCE_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# Inline Axiom HTTP client (same pattern as g1_smoke_check.py)
# ---------------------------------------------------------------------------

class AxiomQueryClient:
    """Lightweight Axiom APL query client (tabular format)."""

    def __init__(
        self,
        api_key: str,
        dataset: str,
        base_url: str = "https://api.axiom.co",
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self.dataset = dataset

    @staticmethod
    def _tabular_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten Axiom tabular response to list[dict].

        Response shape: {"tables": [{"fields": [...], "columns": [[...], ...]}]}
        """
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
            print(f"[error] Axiom auth failed (HTTP {resp.status_code})", file=sys.stderr)
            raise SystemExit(2)
        if resp.status_code >= 400:
            print(f"[debug] APL: {apl!r}", file=sys.stderr)
            print(f"[debug] response: {resp.text!r}", file=sys.stderr)
        resp.raise_for_status()
        return self._tabular_to_rows(resp.json())

    async def aclose(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------
# APL query builders (F1 / F2 / F3)
# ---------------------------------------------------------------------------

def build_f1_stale_signals(dataset: str, window_start: str, window_end: str) -> str:
    """F1: stale signal subset — adapted to actual field names.

    Events use top-level "cell" (= cell_id) + payload.strategy (separate fields),
    not the spec's conceptual "payload.cell_key".
    """
    return f"""
['{dataset}']
| where _time >= datetime({window_start}) and _time <= datetime({window_end})
| where event_type == "signal"
| where cell in ("fUSD_p30", "fUST_p30")
| where ['payload.strategy'] in ("rate_percentile", "mean_reversion")
| where ['payload.is_stale'] == true
| project _time, cell, ['payload.strategy'], ['payload.signal_score'],
          ['payload.stale_seconds'], ['payload.candle_close']
""".strip()


def build_f2_fresh_signals(dataset: str, window_start: str, window_end: str) -> str:
    """F2: fresh signal baseline."""
    return f"""
['{dataset}']
| where _time >= datetime({window_start}) and _time <= datetime({window_end})
| where event_type == "signal"
| where cell in ("fUSD_p30", "fUST_p30")
| where ['payload.strategy'] in ("rate_percentile", "mean_reversion")
| where ['payload.is_stale'] == false
| project _time, cell, ['payload.strategy'], ['payload.signal_score']
""".strip()


def build_f3_sparseness_rate(dataset: str, window_start: str, window_end: str) -> str:
    """F3: hard tier degraded events — stale_exceeded per cell x per day."""
    return f"""
['{dataset}']
| where _time >= datetime({window_start}) and _time <= datetime({window_end})
| where event_type == "health_check"
| where ['payload.check_target'] == "signal_pipeline"
| where ['payload.reason'] == "stale_exceeded"
| summarize stale_count_=count() by bin(_time, 1d), cell
""".strip()


def build_decisions_query(dataset: str, window_start: str, window_end: str) -> str:
    """Decision events with pnl_proxy for win/loss join (Phase 4.1 schema)."""
    return f"""
['{dataset}']
| where _time >= datetime({window_start}) and _time <= datetime({window_end})
| where event_type == "decision"
| where cell in ("fUSD_p30", "fUST_p30")
| where ['payload.strategy'] in ("rate_percentile", "mean_reversion")
| project _time, cell, ['payload.strategy'], ['payload.pnl_proxy']
""".strip()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def query_stale_signals(
    client: AxiomQueryClient, window_start: str, window_end: str,
) -> list[dict[str, Any]]:
    """F1 query."""
    return await client.query_apl(build_f1_stale_signals(client.dataset, window_start, window_end))


async def query_fresh_signals(
    client: AxiomQueryClient, window_start: str, window_end: str,
) -> list[dict[str, Any]]:
    """F2 query."""
    return await client.query_apl(build_f2_fresh_signals(client.dataset, window_start, window_end))


async def query_decision_events(
    client: AxiomQueryClient, window_start: str, window_end: str,
) -> list[dict[str, Any]]:
    """Decision events for win/loss join."""
    return await client.query_apl(build_decisions_query(client.dataset, window_start, window_end))


async def query_sparseness_rate(
    client: AxiomQueryClient, window_start: str, window_end: str,
) -> list[dict[str, Any]]:
    """F3 query."""
    return await client.query_apl(build_f3_sparseness_rate(client.dataset, window_start, window_end))


# ---------------------------------------------------------------------------
# Join + verdict logic
# ---------------------------------------------------------------------------

def _parse_time(t: str | None) -> float:
    """Parse ISO timestamp string to unix seconds. Returns 0.0 on failure."""
    if not t:
        return 0.0
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def join_stale_with_decisions(
    stale_signals: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """For each stale signal, find matching decision (cell + strategy) within tolerance.

    Decision events use Axiom-flattened field names:
      - "cell"               → top-level cell_id
      - "payload.strategy"   → strategy name

    Returns list of {cell, strategy, is_win, pnl_proxy}.
    """
    joined: list[dict[str, Any]] = []
    for sig in stale_signals:
        sig_time = _parse_time(sig.get("_time"))
        sig_cell = sig.get("cell")
        sig_strategy = sig.get("payload.strategy")
        if not (sig_cell and sig_strategy):
            continue

        match = next(
            (
                d for d in decisions
                if d.get("cell") == sig_cell
                and d.get("payload.strategy") == sig_strategy
                and abs(_parse_time(d.get("_time")) - sig_time) <= DECISION_JOIN_TOLERANCE_SECONDS
            ),
            None,
        )
        if match:
            pnl = float(match.get("payload.pnl_proxy") or 0.0)
            joined.append({
                "cell": sig_cell,
                "strategy": sig_strategy,
                "is_win": pnl > 0,
                "pnl_proxy": pnl,
            })
    return joined


def compute_verdict(
    joined_stale: list[dict[str, Any]],
    cell: str,
    strategy: str,
) -> dict[str, Any]:
    """Apply Phase 3b qualification gate to stale subset for one (cell, strategy) pair.

    Gate: n >= 30 AND win_pct >= 0.60 AND margin >= 0.05
    Verdicts:
      GREEN  — qualifies
      YELLOW — insufficient sample OR win_pct in [0.55, 0.60)
      RED    — win_pct < 0.55 OR margin < 0.0
    """
    pair_signals = [
        s for s in joined_stale
        if s["cell"] == cell and s["strategy"] == strategy
    ]
    n = len(pair_signals)

    if n == 0:
        return {
            "cell": cell,
            "strategy": strategy,
            "n_stale": 0,
            "verdict": "YELLOW",
            "rationale": "no stale signals joined to decisions — verify F3 sparseness or extend window",
        }

    if n < MIN_SAMPLE_SIZE:
        return {
            "cell": cell,
            "strategy": strategy,
            "n_stale": n,
            "verdict": "YELLOW",
            "rationale": f"sample size {n} < {MIN_SAMPLE_SIZE} — extend window 1-2 weeks",
        }

    wins = sum(1 for s in pair_signals if s["is_win"])
    win_pct = wins / n
    margin = sum(s["pnl_proxy"] for s in pair_signals) / n

    if win_pct >= WIN_RATE_FLOOR and margin >= MARGIN_FLOOR:
        verdict = "GREEN"
        rationale = f"qualifies: win_pct={win_pct:.2%} >= {WIN_RATE_FLOOR:.0%}, margin={margin:.4f} >= {MARGIN_FLOOR}"
    elif win_pct >= YELLOW_WIN_RATE_THRESHOLD:
        verdict = "YELLOW"
        rationale = (
            f"close to threshold: win_pct={win_pct:.2%} in [{YELLOW_WIN_RATE_THRESHOLD:.0%}, {WIN_RATE_FLOOR:.0%})"
        )
    else:
        verdict = "RED"
        rationale = f"below threshold: win_pct={win_pct:.2%} < {YELLOW_WIN_RATE_THRESHOLD:.0%}"

    return {
        "cell": cell,
        "strategy": strategy,
        "n_stale": n,
        "wins": wins,
        "win_pct": win_pct,
        "margin": margin,
        "verdict": verdict,
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def emit_markdown(
    verdicts: list[dict[str, Any]],
    sparseness: list[dict[str, Any]],
    window_start: str,
    window_end: str,
    output_path: Path,
) -> None:
    """Emit GREEN/YELLOW/RED matrix + sparseness rate table to markdown."""
    now = datetime.now(UTC).isoformat()
    lines: list[str] = [
        f"# Phase 4.3 LOCF G2 Calibration Audit — {window_start} to {window_end}",
        "",
        f"**Generated**: {now}",
        "",
        "## Stale subset verdict per (cell, strategy) pair",
        "",
        "Qualification gate: n ≥ 30 AND win_pct ≥ 60% AND margin ≥ 0.05",
        "",
        "| Cell | Strategy | n_stale | wins | win % | margin | Verdict | Rationale |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for v in verdicts:
        n = v["n_stale"]
        wins_str = str(v.get("wins", "-")) if n >= MIN_SAMPLE_SIZE else "-"
        win_pct_str = f"{v['win_pct']:.2%}" if "win_pct" in v else "-"
        margin_str = f"{v['margin']:.4f}" if "margin" in v else "-"
        rationale = v.get("rationale", "")
        lines.append(
            f"| {v['cell']} | {v['strategy']} | {n} | "
            f"{wins_str} | {win_pct_str} | {margin_str} | "
            f"**{v['verdict']}** | {rationale} |"
        )

    lines += [
        "",
        "## Sparseness rate per cell x per day (F3: stale_exceeded events)",
        "",
    ]

    if sparseness:
        lines += [
            "| Date | Cell | Stale events |",
            "|---|---|---|",
        ]
        for s in sparseness:
            lines.append(
                f"| {s.get('_time', '?')} | {s.get('cell', '?')} | {s.get('stale_count_', 0)} |"
            )
    else:
        lines.append("_No stale_exceeded health_check events found in window._")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n")
    print(f"[g2_audit] Report written → {output_path}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_audit(
    client: AxiomQueryClient,
    window_start: str,
    window_end: str,
    output_path: Path,
) -> None:
    """Run full G2 audit: F1+F2+F3+decisions → verdicts → markdown report."""
    print(f"[g2_audit] Querying Axiom ({client.dataset}) for {window_start} → {window_end}")

    stale, fresh, decisions, sparseness = await asyncio.gather(
        query_stale_signals(client, window_start, window_end),
        query_fresh_signals(client, window_start, window_end),
        query_decision_events(client, window_start, window_end),
        query_sparseness_rate(client, window_start, window_end),
    )

    print(f"[g2_audit] stale={len(stale)} fresh={len(fresh)} decisions={len(decisions)} sparseness={len(sparseness)}")

    joined_stale = join_stale_with_decisions(stale, decisions)
    print(f"[g2_audit] joined_stale={len(joined_stale)}")

    verdicts = [
        compute_verdict(joined_stale, cell, strategy)
        for cell, strategy in AUDIT_PAIRS
    ]

    for v in verdicts:
        print(f"[g2_audit] {v['cell']} x {v['strategy']}: {v['verdict']}  (n={v['n_stale']})")

    emit_markdown(verdicts, sparseness, window_start, window_end, output_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4.3 G2 calibration audit script")
    ap.add_argument("--window-start", required=True, help="ISO8601 start, e.g. 2026-05-20T00:00:00Z")
    ap.add_argument("--window-end", required=True, help="ISO8601 end, e.g. 2026-06-03T00:00:00Z")
    ap.add_argument(
        "--output",
        type=Path,
        default=Path("docs/research/phase4.3_locf_g2_audit.md"),
        help="Output markdown path",
    )
    args = ap.parse_args()

    axiom_api_key = os.environ.get("AXIOM_API_KEY", "")
    axiom_dataset = os.environ.get("AXIOM_DATASET", "bfx-funding-bot")
    if not axiom_api_key:
        print("ERROR: AXIOM_API_KEY env var required", file=sys.stderr)
        sys.exit(2)

    client = AxiomQueryClient(api_key=axiom_api_key, dataset=axiom_dataset)

    async def _run() -> None:
        try:
            await run_audit(client, args.window_start, args.window_end, args.output)
        except SystemExit:
            raise
        finally:
            await client.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
