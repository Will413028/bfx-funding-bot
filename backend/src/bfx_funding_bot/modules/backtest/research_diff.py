"""Week-over-week research diff (Proposal E, 2026-09-22 review §7) — pure, no DB.

Reads the JSON sidecars the research runners already write and answers two
pre-registered questions (strategy registry, 2026-09-22):

- **Champion drift**: an arm whose median over its most recent `recent` windows
  falls below the p25 of its full-history window distribution is flagged. It is
  a review trigger, not a verdict.
- **Challenger overtake**: a paired comparison whose mean-difference 95% CI has a
  lower bound above 0 in each of the last `streak` consecutive reports is flagged
  so the registry re-open conditions get checked. Never auto-promotes.

Supported kinds: `period-structure` (`scripts.run_period_structure_backtest`) and
`oos` (`scripts.run_oos_profitability`; carries no paired CI, so only drift
applies). Reports are compared in the chronological order given.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median
from typing import Any

Kind = str  # "period-structure" | "oos"
KINDS: tuple[str, ...] = ("period-structure", "oos")


@dataclass(frozen=True)
class ArmSnapshot:
    label: str
    median_monthly: Decimal
    p25_monthly: Decimal
    annualized_pct: Decimal
    windows: tuple[Decimal, ...] = ()  # net_monthly per window, chronological; may be empty


@dataclass(frozen=True)
class PairSnapshot:
    label: str
    mean_active: Decimal
    ci_lo: Decimal
    ci_hi: Decimal


@dataclass(frozen=True)
class ReportSnapshot:
    label: str
    arms: dict[str, ArmSnapshot] = field(default_factory=dict)
    pairs: dict[str, PairSnapshot] = field(default_factory=dict)


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def parse_period_structure(payload: Mapping[str, Any], *, label: str) -> ReportSnapshot:
    arms: dict[str, ArmSnapshot] = {}
    pairs: dict[str, PairSnapshot] = {}
    for sym in payload.get("symbols", []):
        symbol = sym["symbol"]
        windows = sym.get("windows", {})
        for name, s in sym.get("arms", {}).items():
            key = f"{symbol}:{name}"
            arms[key] = ArmSnapshot(
                label=key, median_monthly=_d(s["median_monthly"]),
                p25_monthly=_d(s["p25_monthly"]), annualized_pct=_d(s["annualized_pct"]),
                windows=tuple(_d(v) for _, v in windows.get(name, [])),
            )
        for name, p in sym.get("pairs", {}).items():
            key = f"{symbol}:{name}"
            lo, hi = p["mean_ci"]
            pairs[key] = PairSnapshot(label=key, mean_active=_d(p["mean_active"]),
                                      ci_lo=_d(lo), ci_hi=_d(hi))
    return ReportSnapshot(label=label, arms=arms, pairs=pairs)


def parse_oos(payload: Mapping[str, Any], *, label: str) -> ReportSnapshot:
    arms: dict[str, ArmSnapshot] = {}
    for r in payload.get("reports", []):
        cell = r["cell"]
        windows = r.get("windows", {})
        for side in ("strategy", "baseline"):
            s = r[side]
            key = f"{cell}:{side}"
            arms[key] = ArmSnapshot(
                label=key, median_monthly=_d(s["median_monthly"]),
                p25_monthly=_d(s["p25_monthly"]), annualized_pct=_d(s["annualized_pct"]),
                windows=tuple(_d(v) for _, v in windows.get(side, [])),
            )
    return ReportSnapshot(label=label, arms=arms)  # OOS carries no paired CI


def parse_report(kind: Kind, payload: Mapping[str, Any], *, label: str) -> ReportSnapshot:
    if kind == "period-structure":
        return parse_period_structure(payload, label=label)
    if kind == "oos":
        return parse_oos(payload, label=label)
    raise ValueError(f"unsupported kind {kind!r}; expected one of {KINDS}")


def champion_drift(arm: ArmSnapshot, *, recent: int = 12) -> bool | None:
    """True when the recent-window median sits below the full-history p25; None when
    the report carries no per-window data or fewer than `recent` windows."""
    if len(arm.windows) < recent:
        return None
    recent_median = _d(median(arm.windows[-recent:]))
    return recent_median < arm.p25_monthly


def consecutive_overtakes(history: Sequence[ReportSnapshot], *, streak: int = 3) -> list[str]:
    """Pair labels whose CI lower bound is > 0 in each of the last `streak` reports."""
    if len(history) < streak:
        return []
    tail = history[-streak:]
    labels = set(tail[-1].pairs)
    out: list[str] = []
    for label in sorted(labels):
        if all(label in r.pairs and r.pairs[label].ci_lo > 0 for r in tail):
            out.append(label)
    return out


def _f(x: Decimal, nd: int = 4) -> str:
    return "inf" if not x.is_finite() else f"{x:.{nd}f}"


def render_markdown(
    history: Sequence[ReportSnapshot], *, kind: Kind, recent: int = 12, streak: int = 3
) -> str:
    if not history:
        return "# Weekly research diff\n\n_no reports_\n"
    current = history[-1]
    previous = history[-2] if len(history) > 1 else None
    drifts = {k: champion_drift(a, recent=recent) for k, a in current.arms.items()}
    overtakes = consecutive_overtakes(history, streak=streak)

    lines = [
        f"# Weekly research diff — {kind}",
        "",
        f"**Current**: {current.label}" + (f" · **Previous**: {previous.label}" if previous else ""),
        f"**Reports in history**: {len(history)} · drift rule: median(last {recent} windows) < "
        f"full-history p25 · overtake rule: pair CI lower bound > 0 for {streak} consecutive reports",
        "",
        "## Flags",
        "",
    ]
    flagged = [k for k, v in drifts.items() if v]
    lines.append(f"- **Champion drift**: {', '.join(flagged) if flagged else 'none'}")
    unknown = [k for k, v in drifts.items() if v is None]
    if unknown:
        lines.append(f"- drift not evaluable (no per-window data or < {recent} windows): "
                     + ", ".join(unknown))
    if kind == "oos":
        lines.append("- **Challenger overtake**: not applicable (OOS reports carry no paired CI)")
    else:
        lines.append(f"- **Challenger overtake** ({streak} consecutive): "
                     + (", ".join(overtakes) if overtakes else "none"))
    lines.append("- Nothing here promotes anything; a flag opens a registry review.")

    lines += ["", "## Arms — current vs previous", "",
              "| arm | annualized % | Δ | median monthly % | Δ | p25 | drift |", "|---|---|---|---|---|---|---|"]
    for key in sorted(current.arms):
        a = current.arms[key]
        p = previous.arms.get(key) if previous else None
        d_ann = _f(a.annualized_pct - p.annualized_pct, 2) if p else "-"
        d_med = _f(a.median_monthly - p.median_monthly) if p else "-"
        flag = {True: "DRIFT", False: "ok", None: "n/a"}[drifts[key]]
        lines.append(f"| {key} | {_f(a.annualized_pct, 2)} | {d_ann} | {_f(a.median_monthly)} | "
                     f"{d_med} | {_f(a.p25_monthly)} | {flag} |")
    if current.pairs:
        lines += ["", "## Pairs — current", "", "| pair | mean Δ | 95% CI | streak |", "|---|---|---|---|"]
        for key in sorted(current.pairs):
            pr = current.pairs[key]
            run = 0
            for r in reversed(history):
                if key in r.pairs and r.pairs[key].ci_lo > 0:
                    run += 1
                else:
                    break
            lines.append(f"| {key} | {_f(pr.mean_active)} | [{_f(pr.ci_lo)}, {_f(pr.ci_hi)}] | {run} |")
    return "\n".join(lines) + "\n"
