"""Current profiles exercise loaders; old moving-main deploy tests are superseded
by tests/scripts/test_release_package.py and the real application launch fixture.
"""
from pathlib import Path

import pytest

from bfx_funding_bot.core.release_identity import runtime_environment
from bfx_funding_bot.modules.marketfeed.config import load_cells_only, load_config
from scripts.immutable_release import read_env

ROOT = Path(__file__).resolve().parents[4]


def test_shadow_p14_is_shadow_only():
    env = read_env(ROOT / "deploy/vm/shadow-p14.env")
    assert env["BFX_PHASE"] == "shadow"
    assert env["BFX_DEPLOYMENT_ENV"] == "shadow"
    assert env["BFX_EXECUTION_POLICY"] == "optimizer_shadow"
    cells = load_cells_only(ROOT / "backend/configs/cells.experimental-p14.yaml")
    assert cells


def test_live_profile_loads_without_legacy_money_or_canary_scope(monkeypatch):
    env = read_env(ROOT / "deploy/vm/live.env")
    assert runtime_environment(env) == env
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture:fixture@localhost/fixture")
    config = load_config(cells_yaml_path=ROOT / "backend/configs/cells.live.yaml")
    assert config.phase.value == "live"
    assert [cell.cell_id for cell in config.cells] == ["fUST_a30", "fUST_p2"]
    from bfx_funding_bot.modules.execution.safety.config import load_safety_config
    from bfx_funding_bot.modules.marketfeed.daemon import assert_canary_guard_invariant
    safety = load_safety_config(ROOT / "backend/configs/safety.live.yaml")
    assert_canary_guard_invariant(config.phase, safety)


def test_legacy_canary_environment_cannot_create_runtime_authority():
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryProfile, CanaryStartupBlocked
    with pytest.raises(CanaryStartupBlocked, match="legacy_canary"):
        CanaryProfile.from_environ({"BFX_CANARY_AMOUNT_USDT": "150",
            "BFX_CANARY_CAP_USDT": "150", "BFX_CANARY_MAX_EVIDENCE_AGE_SECONDS": "300",
            "BFX_CANARY_ACCOUNT_ID": "11111111-1111-4111-8111-111111111111",
            "BFX_CANARY_ENVIRONMENT": "prod", "BFX_CANARY_SYMBOL": "fUST",
            "BFX_CANARY_CELL": "fUST_a30", "BFX_CANARY_STRATEGY": "mean_reversion"})
