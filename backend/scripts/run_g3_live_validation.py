"""G3 — live bot-vs-idle validation of the deployed canary MR config.

Loads the venue credit model (credits attributed to the bot's cells through
funding_trades, ledger payouts) and the market-rate series (funding_candles
.close, funding_stats) from Postgres (VM 自托 bfx-postgres；2026-06-23 前為 Neon);
attributes the bot's absolute return on the capital budget vs idle (primary)
plus MR timing alpha vs AlwaysMarketRate (secondary, non-gating diagnostic) via
modules/live_validation, reuses the oos_profitability metrics, and writes a
markdown + JSON report with a four-state verdict.

Since 2026-09-27 the active arm is venue credit interest with actual held time
and C is the ledger funding-wallet balance; earlier reports are not comparable
(see the report's "Methodology change" section).

Run from backend/:
  uv run python -m scripts.run_g3_live_validation --out docs/research/<date>-g3-live-validation.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.live_validation.live_attribution import (
    METHODOLOGY_CHANGE_DATE,
    G3Report,
)
from bfx_funding_bot.modules.live_validation.weekly_attribution import FEE_RATE as _FEE_RATE


def _parse_capital(raw: str | None) -> Decimal | None:
    """--capital: an explicit C overrides the ledger-derived one.

    There is no environment default any more: BFX_ALLOCATION_CAP_USDT is 0 in
    production since the capital policy replaced allocation caps (2026-09-27
    weekly run died on "capital must be positive"). Without --capital, C per
    window is the funding-wallet balance from funding_interest_payments."""
    if raw is None:
        return None
    capital = Decimal(raw)
    if capital <= 0:
        raise SystemExit(f"--capital must be positive, got {raw}")
    return capital


def _capital_label(source: str) -> str:
    if source == "ledger":
        return "funding-wallet balance per weekly window (venue ledger)"
    return f"fixed, from {source}"


def _methodology_section(report: G3Report) -> list[str]:
    return [
        f"## Methodology change ({METHODOLOGY_CHANGE_DATE})",
        f"- **Discontinuity: G3 reports dated before {METHODOLOGY_CHANGE_DATE} are not"
        " comparable with this one.** Do not diff headline, CI or verdict across the"
        " change; this report recomputes the whole history under the new model, so"
        " its own weekly series is consistent.",
        "- Interest (active arm): was ORDER_FILL size × fill rate × assumed"
        " held-to-term (2 days, capped by CREDIT_CLOSED), booked to the week of the"
        " fill. Now: venue credits (funding_credit_history + open venue_credit_state)"
        " × credit rate × actual held time MTS_OPENING..MTS_LAST_PAYOUT (..now while"
        " open), split across the weeks it spans. The old model overstated early"
        " repayments: two 150.77 fUST credits repaid after 14 min were booked as 2 days.",
        "- Bot scope: credits that funding_trades lead to one of our offers and its"
        " execution-decision cell; the rest are reported as unattributed and excluded.",
        f"- Capital base C: was the fixed BFX_ALLOCATION_CAP_USDT; now"
        f" {_capital_label(report.capital_source)}.",
        "- Trust gate: was a point-in-time deployed-principal anchor vs position_state"
        " (plus a NAV anchor that was never available); now the weekly ledger"
        " reconciliation below. Open principal is read from the venue, so the old"
        " anchor no longer tested the model.",
        "- Over-deploy clamp removed: actual held times leave no re-lent principal"
        " to double count; the peak is reported instead (Deployment check).",
    ]


def _coverage_section(report: G3Report) -> list[str]:
    cov = report.coverage
    lines = [
        "## Credit coverage",
        f"- bot credits: {cov.bot_credits} (ambiguous pairing: {cov.ambiguous_credits},"
        " counted in their paired cell)",
    ]
    lines += [f"- {cell}: gross interest {gross:.8f}" for cell, gross in cov.gross_by_cell.items()]
    lines.append(
        f"- unattributed (no trade / offer not ours, excluded): {cov.unattributed_credits}"
        f" credits, gross interest {cov.unattributed_gross:.8f}"
    )
    return lines


def _reconciliation_section(report: G3Report) -> list[str]:
    lines = ["## Ledger reconciliation (trust gate)"]
    if not report.reconciliation_available:
        return [*lines, "- **unavailable** — no ledger payouts; the credit model is unchecked."]
    complete = [r for r in report.reconciliations if r.complete]
    flagged = [r for r in complete if r.flagged]
    lines += [
        f"- complete weeks: {len(complete)}, flagged: {len(flagged)}; "
        f"incomplete (payouts not all in yet): {len(report.reconciliations) - len(complete)}",
        "- credits net (×0.85) vs ledger payouts in the week shifted one day;"
        " flag: |diff| > max(5% of ledger, 0.01). Any flag → UNRELIABLE.",
        "",
        "| week (UTC) | credits net | ledger net | diff | status |",
        "|---|---:|---:|---:|---|",
    ]
    for r in report.reconciliations:
        week = datetime.fromtimestamp(r.week_start_ms / 1000, UTC).strftime("%Y-%m-%d")
        status = "FLAG" if r.flagged else ("ok" if r.complete else "incomplete")
        lines.append(f"| {week} | {r.credit_net:.6f} | {r.ledger_net:.6f} "
                     f"| {r.diff:+.6f} | {status} |")
    return lines


def render_markdown(*, report: G3Report, fee_rate: Decimal) -> str:
    """Pure renderer — unit-testable without a DB."""
    verdict, frr, dep = report.verdict, report.frr, report.deployment
    honesty = [
        "## Honesty caveats",
        "- bot-vs-idle PASS asserts the bot beats an idle balance (earns the market"
        " rate), NOT that MR timing beats always-lending — see the MR alpha diagnostic.",
        "- Interest is gross (credit rate × held time); the fee-adjusted line applies"
        " Bitfinex's 15%. The ledger reconciliation checks the net figure.",
        "- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.",
        "- Platform/credit tail (Bitfinex/Tether) is uncapturable here — bounded by the capital policy.",
    ]
    if dep.over_deployed:
        honesty.append(
            f"- Bot credits peaked at {dep.peak_open_principal} open principal"
            f" ({dep.over_deploy_factor:.2f}x C={dep.cap}): the return on C overstates"
            " a C-sized budget (no clamp applied)."
        )

    idle_arm_note = (
        "- idle arm = 0% by construction; the headline is the bot's own realized"
        " return on the capital budget."
    )
    if verdict.mr_alpha_available:
        mr_alpha = [
            "## MR timing alpha (secondary diagnostic)",
            f"- MR alpha spread (active − AlwaysMarketRate): {verdict.mr_alpha_spread}%",
            f"- MR alpha 95% CI: [{verdict.mr_alpha_ci_lo}, {verdict.mr_alpha_ci_hi}]",
            "- Diagnostic only — does NOT gate the verdict. Near 0 means MR timing adds"
            " little over always-lending; the product value is bot-vs-idle.",
            idle_arm_note,
        ]
    else:
        mr_alpha = [
            "## MR timing alpha (secondary diagnostic)",
            "- unavailable — no in-band market-rate coverage in window (see reasons)."
            " bot-vs-idle (idle ≡ 0) is unaffected.",
            idle_arm_note,
        ]

    frr_section = ["", "## AlwaysFRR benchmark (value bar)"]
    if frr.available:
        frr_section += [
            f"- bot − AlwaysFRR spread: {frr.spread}%  (95% CI [{frr.ci_lo}, {frr.ci_hi}])",
            "- Bar: 贏不了免費的 FRR auto-renew = 零附加值。舊的「per-symbol cap 加碼前"
            " spread 需非負且 verdict PASS」是歷史政策（capital policy 下沒有固定 cap，"
            "見 ARCHITECTURE.md），本段是證據、不是自動 gate。",
            "- Rate source: funding_stats.frr × 365 (≈ ticker per-day FRR;"
            " band-guarded, see live_attribution.FRR_ANNUALIZATION).",
        ]
    else:
        frr_section += [
            f"- **unavailable** — {frr.reason}",
            "- 無證據：不能據此主張 bot 優於 FRR auto-renew。",
        ]

    lines = [
        "# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5",
        "",
        f"Data window: {report.data_window} | bot credits: {report.coverage.bot_credits}"
        f" | weekly windows: {verdict.n_windows}",
        f"Model: venue credits × actual held time (since {METHODOLOGY_CHANGE_DATE};"
        " not comparable with earlier reports) | C: "
        f"{_capital_label(report.capital_source)}",
        "",
        "## TL;DR",
        f"- **Verdict: {verdict.state.value}**",
        f"- Headline bot-vs-idle (absolute return on C since inception): {verdict.headline_bot_vs_idle}%",
        f"- bot-vs-idle 95% CI: [{verdict.ci_lo}, {verdict.ci_hi}]",
        f"- Headline bot-vs-idle **fee-adjusted** (×{Decimal('1') - fee_rate}): "
        f"{verdict.headline_bot_vs_idle * (Decimal('1') - fee_rate)}%"
        " — Bitfinex takes 15% of interest",
        "",
        "### Reasons",
        *[f"- {r}" for r in verdict.reasons],
        "",
        *_methodology_section(report),
        "",
        *_coverage_section(report),
        "",
        *_reconciliation_section(report),
        "",
        "## Deployment check",
        f"- peak open principal of bot credits: {dep.peak_open_principal} vs C={dep.cap}"
        f" → {'OVER C' if dep.over_deployed else 'within C'}",
        "",
        *mr_alpha,
        *frr_section,
        "",
        *honesty,
        "",
        "## Recommendation",
        "- PASS: bot reliably beats idle (earns the market rate on the budget); scale-up is the operator's call.",
        "- INSUFFICIENT_DATA: keep accruing credits/windows; re-run after more weekly windows.",
        "- FAIL: bot does not beat idle (negative-rate regime or realized loss) — investigate before scaling.",
        "- UNRELIABLE: credit interest diverges from the venue ledger — fix attribution before trusting.",
    ]
    return "\n".join(lines)


def _verdict_to_json(report: G3Report, *, fee_rate: Decimal) -> dict[str, object]:
    v, frr, dep, cov = report.verdict, report.frr, report.deployment, report.coverage
    return {
        "methodology": {
            "model": "venue_credits_actual_held_time",
            "since": METHODOLOGY_CHANGE_DATE,
            "comparable_with_reports_before": False,
            "capital_source": report.capital_source,
        },
        "state": v.state.value,
        "headline_bot_vs_idle": str(v.headline_bot_vs_idle),
        "n_windows": v.n_windows,
        "ci_lo": str(v.ci_lo),
        "ci_hi": str(v.ci_hi),
        "reasons": v.reasons,
        "mr_alpha": {
            "spread": str(v.mr_alpha_spread),
            "ci_lo": str(v.mr_alpha_ci_lo),
            "ci_hi": str(v.mr_alpha_ci_hi),
            "available": v.mr_alpha_available,
        },
        "fee_adjusted": {
            "fee_rate": str(fee_rate),
            "headline_bot_vs_idle_net": str(
                v.headline_bot_vs_idle * (Decimal("1") - fee_rate)
            ),
        },
        "frr_benchmark": {
            "available": frr.available,
            "spread": str(frr.spread),
            "ci_lo": str(frr.ci_lo),
            "ci_hi": str(frr.ci_hi),
            "reason": frr.reason,
        },
        "credit_coverage": {
            "bot_credits": cov.bot_credits,
            "gross_by_cell": {cell: str(g) for cell, g in cov.gross_by_cell.items()},
            "unattributed_credits": cov.unattributed_credits,
            "unattributed_gross": str(cov.unattributed_gross),
            "ambiguous_credits": cov.ambiguous_credits,
        },
        "reconciliation": {
            "available": report.reconciliation_available,
            "weeks": [
                {
                    "week_start_ms": r.week_start_ms,
                    "credit_net": str(r.credit_net),
                    "ledger_net": str(r.ledger_net),
                    "diff": str(r.diff),
                    "complete": r.complete,
                    "flagged": r.flagged,
                }
                for r in report.reconciliations
            ],
        },
        "deployment": {
            "cap": str(dep.cap),
            "peak_open_principal": str(dep.peak_open_principal),
            "over_deployed": dep.over_deployed,
        },
    }


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output .md path")
    parser.add_argument(
        "--capital",
        default=None,
        help="capital budget C (default: per-window funding-wallet balance from the venue ledger)",
    )
    args = parser.parse_args()

    from scripts._g3_loaders import build_verdict_from_neon

    report = await build_verdict_from_neon(capital=_parse_capital(args.capital))

    out = Path(args.out)
    out.write_text(render_markdown(report=report, fee_rate=_FEE_RATE))
    out.with_suffix(".json").write_text(
        json.dumps(_verdict_to_json(report, fee_rate=_FEE_RATE), indent=2)
    )
    print(f"wrote {out} and {out.with_suffix('.json')}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
