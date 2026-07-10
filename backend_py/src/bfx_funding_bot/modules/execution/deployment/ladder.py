"""Spike-rung ladder policy (observe-only — 2026-07-10 strategy review).

純 policy：把每筆 cell fill 的一小片（spike_fraction）換算成掛在 ask 之上的
1-N 檔 spike rungs。競品（MikaLendingBot/eAndrius/instabot42）用靜態 ladder
替代動態改價；E1+E2 已是動態版，ladder 唯一多出的能力 = spike capture。

目前 OBSERVE-ONLY：reconciler 只 log `ladder_would_post`，submit 行為零改變。
# ponytail: enforce path 刻意不存在 — 需要 reprice-sweep 豁免 tag + clamp
# bypass（E2 UNDERCUT 會把 rung 夷平回 ask−1tick）+ E3 per-rung attribution，
# observe 數據證明 uplift 前不建（見 plan Task 6 context）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LadderPolicy:
    spike_fraction: float        # 0.15 = 每筆 fill 切 15% 給 spike rungs
    rung_multipliers: tuple[float, ...]  # (1.5, 3.0) — rung rate = ask × k
    min_rung_usdt: float         # 153 — venue floor（與 sizing.effective_min 同源）


def ladder_policy_from_env(environ: Mapping[str, str]) -> LadderPolicy | None:
    """None = feature off（預設）。BFX_LADDER_OBSERVE=true 才建 policy。"""
    if environ.get("BFX_LADDER_OBSERVE", "false").lower() not in ("1", "true", "yes"):
        return None
    return LadderPolicy(
        spike_fraction=float(environ.get("BFX_LADDER_SPIKE_FRACTION", "0.15")),
        rung_multipliers=tuple(
            float(x) for x in environ.get("BFX_LADDER_MULTIPLIERS", "1.5,3.0").split(",")
        ),
        min_rung_usdt=float(environ.get("BFX_LADDER_MIN_RUNG_USDT", "153")),
    )


def spike_rungs(
    *, amount: float, ask: float, policy: LadderPolicy,
) -> list[tuple[float, float]]:
    """回傳 [(rung_amount_usdt, rung_rate)]。

    budget = amount × spike_fraction，均分到最多 len(rung_multipliers) 檔；
    每檔必須 ≥ min_rung_usdt（venue floor），不夠就減檔——保留最低 multiplier
    優先（最高成交機率的 rung 最後放棄）。ask 退化（≤0）→ 空。
    """
    if ask <= 0.0:
        return []
    budget = amount * policy.spike_fraction
    for n in range(len(policy.rung_multipliers), 0, -1):
        per_rung = budget / n
        if per_rung >= policy.min_rung_usdt:
            # round(..., 10): same float-noise guard as book_clamp's
            # `round(ticker.ask - TICK, 10)` — ask×k lands on values like
            # 0.00030000000000000003 without it (IEEE-754, not a real rate).
            return [
                (per_rung, round(ask * k, 10)) for k in policy.rung_multipliers[:n]
            ]
    return []
