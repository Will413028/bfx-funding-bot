"""Deterministic deploy sanity gate for yield/carry cells.

A deployed config must (a) be distinguishable from the passive AlwaysMarketRate
baseline (it actually acts) and (b) not be worse than passive (mean active
return's bootstrap CI lower bound >= 0). Catches the canary-mr-config-inert
failure mode where a shipped config collapses to passive.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GateResult:
    passed: bool
    distinguishable: bool  # (a): acts differently from passive
    not_worse: bool        # (b): not statistically worse than passive
    reasons: tuple[str, ...]


def evaluate_gate(
    *,
    information_ratio: Decimal,
    pct_outperform: Decimal,
    mean_active_ci_low: Decimal,
    strict: bool = False,
) -> GateResult:
    """Apply the two-part deploy gate.

    (a) distinguishable: information_ratio != 0 AND pct_outperform > 0.
        The inert config has IR == 0 and 0% months outperforming -> fails.
    (b) not_worse: bootstrap 95% CI lower bound of mean active return >= 0
        (strict: > 0). On a ~49-window noisy sample the default floor avoids
        rejecting genuine-but-noisy edges (see spec Risks).
    """
    distinguishable = information_ratio != Decimal("0") and pct_outperform > Decimal("0")
    not_worse = mean_active_ci_low > Decimal("0") if strict else mean_active_ci_low >= Decimal("0")

    reasons: list[str] = []
    if not distinguishable:
        reasons.append(
            f"indistinguishable from passive (IR={information_ratio}, "
            f"pct_outperform={pct_outperform})"
        )
    if not not_worse:
        bound = "> 0" if strict else ">= 0"
        reasons.append(f"mean active CI low {mean_active_ci_low} not {bound}")
    return GateResult(
        passed=distinguishable and not_worse,
        distinguishable=distinguishable,
        not_worse=not_worse,
        reasons=tuple(reasons),
    )
