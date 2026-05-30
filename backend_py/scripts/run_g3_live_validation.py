"""G3 — live active-vs-passive validation of the deployed canary MR config.

Loads live fills (event_log), market-rate series (funding_candles.close), and
reconcile state from Neon; attributes active vs passive yield (modules/live_validation), reuses the
oos_profitability metrics, and writes a markdown + JSON report with a four-state verdict.

Run from backend_py/:
  uv run python scripts/run_g3_live_validation.py --out docs/research/<date>-g3-live-validation.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.live_validation.live_attribution import G3Verdict


def render_markdown(*, verdict: G3Verdict, data_window: str, n_fills: int) -> str:
    """Pure renderer — unit-testable without a DB."""
    lines = [
        "# G3 Live Validation — fUST MeanReversion (a30, p2), deployed ema_span=24/thr=0.5",
        "",
        f"Data window: {data_window} | fills: {n_fills} | weekly windows: {verdict.n_windows}",
        "",
        "## TL;DR",
        f"- **Verdict: {verdict.state.value}**",
        f"- Headline active spread (since inception): {verdict.headline_active_spread}%",
        f"- Active-spread 95% CI: [{verdict.ci_lo}, {verdict.ci_hi}]",
        "",
        "### Reasons",
        *[f"- {r}" for r in verdict.reasons],
        "",
        "## Honesty caveats",
        "- Held-to-term duration assumption (matured credits have no close event).",
        "- Fills attributed with the conservative shorter period when cell identity is absent.",
        "- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.",
        "- Platform/credit tail (Bitfinex/Tether) is uncapturable here — mitigated by the cap.",
        "",
        "## Recommendation",
        "- PASS: live alpha confirmed; scale-up is the operator's call.",
        "- INSUFFICIENT_DATA: keep accruing fills; re-run after more weekly windows.",
        "- FAIL: deployed config does not beat passive live — investigate before scaling.",
        "- UNRELIABLE: yield model diverges from venue truth — fix attribution before trusting.",
    ]
    return "\n".join(lines)


def _verdict_to_json(v: G3Verdict) -> dict[str, object]:
    return {
        "state": v.state.value,
        "headline_active_spread": str(v.headline_active_spread),
        "n_windows": v.n_windows,
        "ci_lo": str(v.ci_lo),
        "ci_hi": str(v.ci_hi),
        "reasons": v.reasons,
    }


async def _amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="output .md path")
    parser.add_argument("--capital", default="570", help="capital budget C (canary cap)")
    args = parser.parse_args()

    from scripts._g3_loaders import build_verdict_from_neon

    verdict, data_window, n_fills = await build_verdict_from_neon(capital=Decimal(args.capital))

    out = Path(args.out)
    out.write_text(render_markdown(verdict=verdict, data_window=data_window, n_fills=n_fills))
    out.with_suffix(".json").write_text(json.dumps(_verdict_to_json(verdict), indent=2))
    print(f"wrote {out} and {out.with_suffix('.json')}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
