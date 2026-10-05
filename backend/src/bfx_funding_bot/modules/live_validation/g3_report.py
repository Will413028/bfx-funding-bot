"""Markdown and JSON rendering of a G3Report (pure)."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from bfx_funding_bot.modules.live_validation.live_attribution import (
    GATE_WEEKS,
    METHODOLOGY_CHANGE_DATE,
    G3Report,
    reconciliation_status,
)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


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
        " open), split across the UTC calendar weeks it spans. The old model overstated"
        " early repayments: two 150.77 fUST credits repaid after 14 min were booked as 2 days.",
        "- Bot scope: credits that funding_trades lead to one of our offers and its"
        " execution-decision cell; the rest are reported as unattributed and excluded.",
        f"- Capital base C: was the fixed BFX_ALLOCATION_CAP_USDT; now"
        f" {_capital_label(report.capital_source)}.",
        "- Minimum data: was C × 7 capital-days; now the mean ledger wallet balance over"
        " the evaluated windows × 7 days (see Data sufficiency).",
        "- MR-alpha baseline: was one fUST p2 market-rate series for every fill; now one"
        " per bot cell, matching its period_agg, plus a total row.",
        "- Trust gate: was a point-in-time deployed-principal anchor vs position_state"
        " (plus a NAV anchor that was never available); now the ledger reconciliation of"
        f" the most recent {GATE_WEEKS} settled weeks. Open principal is read from the"
        " venue, so the old anchor no longer tested the model.",
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


def _data_section(report: G3Report) -> list[str]:
    t = report.data_threshold
    return [
        "## Data sufficiency",
        f"- bot capital-days: {t.capital_days:.2f}; minimum {t.minimum:.2f} ="
        f" {t.basis}. Below it the verdict stays INSUFFICIENT_DATA.",
        f"- weekly windows: {report.verdict.n_windows} (minimum 8).",
    ]


def _reconciliation_section(report: G3Report) -> list[str]:
    lines = ["## Ledger reconciliation (trust gate)"]
    if not report.reconciliation_available:
        return [*lines, "- **unavailable** — no ledger payouts; the credit model is unchecked."]
    gate, acks = report.gate_weeks, report.acknowledgements
    statuses = [reconciliation_status(r, gate_weeks=gate, acks=acks)
                for r in report.reconciliations]
    lines += [
        f"- gate: the most recent {GATE_WEEKS} settled weeks"
        + (f" ({_day(gate[0])}..{_day(gate[-1])})" if gate else "")
        + f"; blocking flags: {statuses.count('FLAG')}. A FLAG inside the gate → UNRELIABLE"
        " unless an operator acknowledged that week (--ack-week); older flags are shown"
        " but do not block.",
        "- credits net (×0.85) vs ledger payouts in the week shifted one day;"
        " flag: |diff| > max(5% of ledger, 0.01).",
        "",
        "| week (UTC) | credits net | ledger net | diff | status |",
        "|---|---:|---:|---:|---|",
    ]
    for r, status in zip(report.reconciliations, statuses, strict=True):
        lines.append(f"| {_day(r.week_start_ms)} | {r.credit_net:.6f} | {r.ledger_net:.6f} "
                     f"| {r.diff:+.6f} | {status} |")
    lines += ["", "### Operator acknowledgements"]
    if not acks:
        lines.append("- none")
    flagged_in_gate = {r.week_start_ms for r in report.reconciliations
                       if r.flagged and r.week_start_ms in gate}
    for week, reason in sorted(acks.items()):
        used = "clears a gate FLAG" if week in flagged_in_gate else "unused (no gate FLAG that week)"
        lines.append(f"- {_day(week)}: {reason} — {used}")
    return lines


def _mr_alpha_section(report: G3Report) -> list[str]:
    v = report.verdict
    lines = [
        "## MR timing alpha (secondary diagnostic)",
        "- Per bot cell: its interest vs lending its share of C (its share of the bot's"
        " capital-days) at the market rate of its own period_agg all the time. A cell"
        " without an in-band series for its period is listed as unavailable and left out"
        " of the total; the total states the share of bot capital-days it covers.",
        "- Diagnostic only — does NOT gate the verdict.",
        "",
        "| cell | period | capital share | spread | 95% CI | note |",
        "|---|---|---:|---:|---|---|",
    ]
    for c in report.mr_alpha_cells:
        if c.available:
            lines.append(f"| {c.cell} | {c.period_agg} | {c.capital_share:.2%} | {c.spread}% "
                         f"| [{c.ci_lo}, {c.ci_hi}] | |")
        else:
            lines.append(f"| {c.cell} | {c.period_agg} | {c.capital_share:.2%} | — | — "
                         f"| unavailable: {c.reason} |")
    if v.mr_alpha_available:
        lines.append(f"| **total** | | {report.mr_alpha_coverage:.2%} | {v.mr_alpha_spread}% "
                     f"| [{v.mr_alpha_ci_lo}, {v.mr_alpha_ci_hi}] | covers "
                     f"{report.mr_alpha_coverage:.2%} of bot capital-days |")
    else:
        lines.append("| **total** | | — | — | — | unavailable: no cell has an in-band"
                     " baseline (see reasons); bot-vs-idle (idle ≡ 0) is unaffected |")
    lines.append("- idle arm = 0% by construction; the headline is the bot's own realized"
                 " return on the capital budget.")
    return lines


def render_markdown(*, report: G3Report, fee_rate: Decimal) -> str:
    verdict, frr, dep = report.verdict, report.frr, report.deployment
    honesty = [
        "## Honesty caveats",
        "- bot-vs-idle PASS asserts the bot beats an idle balance (earns the market"
        " rate), NOT that MR timing beats always-lending — see the MR alpha diagnostic.",
        "- Interest is gross (credit rate × held time); the fee-adjusted line applies"
        " Bitfinex's fee. The ledger reconciliation checks the net figure.",
        "- Deployed params are in-sample to the 2022–2026 selection sweep; the live canary is the true OOS.",
        "- Platform/credit tail (Bitfinex/Tether) is uncapturable here — bounded by the capital policy.",
    ]
    if dep.over_deployed:
        honesty.append(
            f"- Bot credits peaked at {dep.peak_open_principal} open principal"
            f" ({dep.over_deploy_factor:.2f}x C={dep.cap}): the return on C overstates"
            " a C-sized budget (no clamp applied)."
        )

    if report.offer_conflicts:
        honesty.append(
            f"- **{len(report.offer_conflicts)} offer cell conflict(s)**: legacy records and the"
            " ledger journal place these venue offers in different cells, so their credits are"
            " unattributed and excluded: " + "; ".join(report.offer_conflicts)
        )

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

    net = Decimal("1") - fee_rate
    lines = [
        "# G3 Live Validation — fUST MeanReversion bot cells",
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
        f"- Headline bot-vs-idle **fee-adjusted** (×{net}): "
        f"{verdict.headline_bot_vs_idle * net}% — Bitfinex takes {fee_rate:.0%} of interest",
        "",
        "### Reasons",
        *[f"- {r}" for r in verdict.reasons],
        "",
        *_methodology_section(report),
        "",
        *_coverage_section(report),
        "",
        *_data_section(report),
        "",
        *_reconciliation_section(report),
        "",
        "## Deployment check",
        f"- peak open principal of bot credits: {dep.peak_open_principal} vs C={dep.cap}"
        f" → {'OVER C' if dep.over_deployed else 'within C'}",
        "",
        *_mr_alpha_section(report),
        *frr_section,
        "",
        *honesty,
        "",
        "## Recommendation",
        "- PASS: bot reliably beats idle (earns the market rate on the budget); scale-up is the operator's call.",
        "- INSUFFICIENT_DATA: keep accruing credits/windows; re-run after more weekly windows.",
        "- FAIL: bot does not beat idle (negative-rate regime or realized loss) — investigate before scaling.",
        "- UNRELIABLE: credit interest diverges from the venue ledger in a recent settled week"
        " — fix attribution, or acknowledge the week with a recorded reason (--ack-week).",
    ]
    return "\n".join(lines)


def verdict_to_json(report: G3Report, *, fee_rate: Decimal) -> dict[str, object]:
    v, frr, dep, cov = report.verdict, report.frr, report.deployment, report.coverage
    gate, acks = report.gate_weeks, report.acknowledgements
    return {
        "methodology": {
            "model": "venue_credits_actual_held_time",
            "since": METHODOLOGY_CHANGE_DATE,
            "comparable_with_reports_before": False,
            "capital_source": report.capital_source,
        },
        "offer_cell_conflicts": list(report.offer_conflicts),
        "state": v.state.value,
        "headline_bot_vs_idle": str(v.headline_bot_vs_idle),
        "n_windows": v.n_windows,
        "ci_lo": str(v.ci_lo),
        "ci_hi": str(v.ci_hi),
        "reasons": v.reasons,
        "data_threshold": {
            "capital_days": str(report.data_threshold.capital_days),
            "minimum": str(report.data_threshold.minimum),
            "basis": report.data_threshold.basis,
        },
        "mr_alpha": {
            "spread": str(v.mr_alpha_spread),
            "ci_lo": str(v.mr_alpha_ci_lo),
            "ci_hi": str(v.mr_alpha_ci_hi),
            "available": v.mr_alpha_available,
            "coverage": str(report.mr_alpha_coverage),
            "cells": [
                {"cell": c.cell, "period_agg": c.period_agg,
                 "capital_share": str(c.capital_share), "available": c.available,
                 "reason": c.reason, "spread": str(c.spread),
                 "ci_lo": str(c.ci_lo), "ci_hi": str(c.ci_hi)}
                for c in report.mr_alpha_cells
            ],
        },
        "fee_adjusted": {
            "fee_rate": str(fee_rate),
            "headline_bot_vs_idle_net": str(v.headline_bot_vs_idle * (Decimal("1") - fee_rate)),
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
            "gate_weeks": [_day(w) for w in gate],
            "acknowledged": {_day(w): reason for w, reason in sorted(acks.items())},
            "weeks": [
                {
                    "week_start": _day(r.week_start_ms),
                    "credit_net": str(r.credit_net),
                    "ledger_net": str(r.ledger_net),
                    "diff": str(r.diff),
                    "complete": r.complete,
                    "flagged": r.flagged,
                    "status": reconciliation_status(r, gate_weeks=gate, acks=acks),
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
