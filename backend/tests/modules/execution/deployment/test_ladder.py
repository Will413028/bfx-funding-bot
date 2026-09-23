"""spike_rungs pure policy — observe-only E3-gated experiment (2026-07-10 review)."""
from bfx_funding_bot.modules.execution.deployment.ladder import (
    LadderPolicy,
    ladder_policy_from_env,
    spike_rungs,
)

POLICY = LadderPolicy(
    spike_fraction=0.15, rung_multipliers=(1.5, 3.0), min_rung_usdt=153.0,
)


def test_two_rungs_when_budget_covers_both():
    # 7000 * 0.15 = 1050 → 525/rung ≥ 153 → two rungs at ask×1.5 / ask×3.0
    rungs = spike_rungs(amount=7000.0, ask=0.0002, policy=POLICY)
    assert rungs == [(525.0, 0.0003), (525.0, 0.0006)]


def test_drops_to_one_rung_when_budget_too_small_for_two():
    # 1200 * 0.15 = 180 → 90/rung < 153 → single rung of the full 180? No:
    # 180 ≥ 153 → one rung at the LOWEST multiplier (highest fill odds).
    rungs = spike_rungs(amount=1200.0, ask=0.0002, policy=POLICY)
    assert rungs == [(180.0, 0.0003)]


def test_no_rungs_when_budget_below_venue_floor():
    # 1000 * 0.15 = 150 < 153 → nothing to post
    assert spike_rungs(amount=1000.0, ask=0.0002, policy=POLICY) == []


def test_no_rungs_on_degenerate_ask():
    assert spike_rungs(amount=7000.0, ask=0.0, policy=POLICY) == []


def test_policy_from_env_defaults_off():
    assert ladder_policy_from_env({}) is None


def test_policy_from_env_observe_on():
    p = ladder_policy_from_env({"BFX_LADDER_OBSERVE": "true"})
    assert p == POLICY
