"""G3 — live bot-vs-idle validation of the deployed canary MR config.

Loads live fills (event_log), market-rate series (funding_candles.close), and
reconcile state from Postgres (VM 自托 bfx-postgres；2026-06-23 前為 Neon); attributes
the bot's absolute return on budget vs idle (primary) plus MR timing alpha vs
AlwaysMarketRate (secondary, non-gating diagnostic) via modules/live_validation,
reuses the oos_profitability metrics, and writes a markdown + JSON report with a
four-state verdict.

Run from backend/:
  uv run python -m scripts.run_g3_live_validation --out /tmp/<date>-g3-live-validation.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.live_validation.live_attribution import (
    ClampDiagnostic,
    FrrBenchmark,
    G3Verdict,
)

# Bitfinex takes 15% of earned interest. Keep in sync with modules/backtest/config.py fee_rate.
_FEE_RATE = Decimal("0.15")


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


def render_markdown(
    *,
    verdict: G3Verdict,
    data_window: str,
    n_fills: int,
    clamp_diag: ClampDiagnostic,
    frr: FrrBenchmark,
    fee_rate: Decimal,
) -> str:
    """Pure renderer — unit-testable without a DB."""
    honesty = [
        "## Honesty caveats",
        "- bot-vs-idle PASS asserts the bot beats an idle balance (earns the market"
        " rate), NOT that MR timing beats always-lending — see the MR alpha diagnostic.",
        "- Held-to-term duration assumption (matured credits have no close event).",
        "- Fills attributed with the conservative shorter period when cell identity is absent.",
        "- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.",
        "- Platform/credit tail (Bitfinex/Tether) is uncapturable here — mitigated by the cap.",
    ]
    if clamp_diag.over_deployed:
        honesty.append(
            f"- Active arm clamped to budget C={clamp_diag.cap}: raw concurrent "
            f"principal peaked at {clamp_diag.peak_concurrent} "
            f"({clamp_diag.over_deploy_factor:.2f}x cap) → "
            f"{clamp_diag.excess_return_pct:.4f}% over-deploy excess removed."
        )

    idle_arm_note = (
        "- idle arm = 0% by construction; the headline is the bot's own realized"
        " return on the allocated budget."
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

    frr_section = ["", "## AlwaysFRR benchmark (cap-increase gating bar)"]
    if frr.available:
        frr_section += [
            f"- bot − AlwaysFRR spread: {frr.spread}%  (95% CI [{frr.ci_lo}, {frr.ci_hi}])",
            "- POLICY: cap 再加碼前，本 spread 需非負（贏不了免費的 FRR auto-renew"
            " = 零附加值）且 verdict 為最新 PASS。",
            "- Rate source: funding_stats.frr × 365 (≈ ticker per-day FRR;"
            " band-guarded, see live_attribution.FRR_ANNUALIZATION).",
        ]
    else:
        frr_section += [
            f"- **unavailable** — {frr.reason}",
            "- POLICY: benchmark unavailable 時 cap 一律不加碼。",
        ]

    lines = [
        "# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5",
        "",
        f"Data window: {data_window} | fills: {n_fills} | weekly windows: {verdict.n_windows}",
        "",
        "## TL;DR",
        f"- **Verdict: {verdict.state.value}**",
        f"- Headline bot-vs-idle (absolute return on budget since inception): {verdict.headline_bot_vs_idle}%",
        f"- bot-vs-idle 95% CI: [{verdict.ci_lo}, {verdict.ci_hi}]",
        f"- Headline bot-vs-idle **fee-adjusted** (×{Decimal('1') - fee_rate}): "
        f"{verdict.headline_bot_vs_idle * (Decimal('1') - fee_rate)}%"
        " — Bitfinex takes 15% of interest; gross headline kept for continuity",
        "",
        "### Reasons",
        *[f"- {r}" for r in verdict.reasons],
        "",
        *mr_alpha,
        *frr_section,
        "",
        *honesty,
        "",
        "## Recommendation",
        "- PASS: bot reliably beats idle (earns the market rate on the budget); scale-up is the operator's call.",
        "- INSUFFICIENT_DATA: keep accruing fills/windows; re-run after more weekly windows.",
        "- FAIL: bot does not beat idle (negative-rate regime or realized loss) — investigate before scaling.",
        "- UNRELIABLE: yield model diverges from venue truth — fix attribution before trusting.",
    ]
    return "\n".join(lines)


def _verdict_to_json(
    v: G3Verdict,
    clamp_diag: ClampDiagnostic,
    *,
    frr: FrrBenchmark,
    fee_rate: Decimal,
) -> dict[str, object]:
    return {
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
        "over_deploy": {
            "cap": str(clamp_diag.cap),
            "peak_concurrent": str(clamp_diag.peak_concurrent),
            "raw_interest": str(clamp_diag.raw_interest),
            "clamped_interest": str(clamp_diag.clamped_interest),
            "detected": clamp_diag.over_deployed,
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

    verdict, data_window, n_fills, clamp_diag, frr = await build_verdict_from_neon(
        capital=_parse_capital(args.capital)
    )

    out = Path(args.out)
    out.write_text(
        render_markdown(
            verdict=verdict, data_window=data_window, n_fills=n_fills, clamp_diag=clamp_diag,
            frr=frr, fee_rate=_FEE_RATE,
        )
    )
    out.with_suffix(".json").write_text(
        json.dumps(_verdict_to_json(verdict, clamp_diag, frr=frr, fee_rate=_FEE_RATE), indent=2)
    )
    print(f"wrote {out} and {out.with_suffix('.json')}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
