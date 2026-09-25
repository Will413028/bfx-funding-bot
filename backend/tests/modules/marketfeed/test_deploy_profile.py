"""The committed deploy profiles load, and parse the way the VM deploy tool reads them."""
import importlib.util
import sys
from pathlib import Path

from bfx_funding_bot.modules.marketfeed.config import load_cells_only, load_config

ROOT = Path(__file__).resolve().parents[4]


def read_env(path: Path) -> dict[str, str]:
    """The deploy tool's own strict parser (deploy/vm/ops/bfx_deploy.py)."""
    spec = importlib.util.spec_from_file_location("vm_ops_bfx_deploy_for_profiles",
                                                  ROOT / "deploy/vm/ops/bfx_deploy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module.parse_env_file(path.read_text(), name=path.name, compose=True)


def test_shadow_p14_is_shadow_only():
    env = read_env(ROOT / "deploy/vm/shadow-p14.env")
    assert env["BFX_PHASE"] == "shadow"
    assert env["BFX_DEPLOYMENT_ENV"] == "shadow"
    assert env["BFX_EXECUTION_POLICY"] == "optimizer_shadow"
    cells = load_cells_only(ROOT / "backend/configs/cells.experimental-p14.yaml")
    assert cells


def test_live_profile_loads_without_legacy_money_or_canary_scope(monkeypatch):
    env = read_env(ROOT / "deploy/vm/live.env")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture:fixture@localhost/fixture")
    config = load_config(cells_yaml_path=ROOT / "backend/configs/cells.live.yaml")
    assert config.phase.value == "live"
    assert [cell.cell_id for cell in config.cells] == ["fUST_a30", "fUST_p2"]
    from bfx_funding_bot.modules.execution.safety.config import load_safety_config
    from bfx_funding_bot.modules.marketfeed.daemon import assert_live_guard_invariant
    safety = load_safety_config(ROOT / "backend/configs/safety.live.yaml")
    assert_live_guard_invariant(config.phase, safety)
