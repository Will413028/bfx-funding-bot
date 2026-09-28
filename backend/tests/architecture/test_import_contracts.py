"""Ratchet import exceptions: lower a constant when ignores are removed.

Never raise a constant to make a new violation pass silently.
"""

import tomllib
from pathlib import Path

CORE_IS_LEAF_MAX_IGNORES = 0
VENUE_BELOW_MODULES_MAX_IGNORES = 19
RUNTIME_NOT_RESEARCH_MAX_IGNORES = 0
MODULES_ACYCLIC_MAX_IGNORES = 35
APPS_IS_TOP_MAX_IGNORES = 0
STRATEGY_IS_PURE_MAX_IGNORES = 0
STRATEGY_LOWER_ONLY_MAX_IGNORES = 0
STRATEGY_NO_INTERNAL_ACCESS_MAX_IGNORES = 0
TRADING_IS_PURE_MAX_IGNORES = 0

MAX_IGNORES_BY_ID = {
    "trading-shadow-no-internal-access": 0,
    "trading-shadow-wiring-is-top": 0,
    "trading-shadow-independent-loader": 0,
    "trading-shadow-no-baseline-adapter": 0,
    "trading-is-pure": TRADING_IS_PURE_MAX_IGNORES,
    "apps-is-top": APPS_IS_TOP_MAX_IGNORES,
    "core-is-leaf": CORE_IS_LEAF_MAX_IGNORES,
    "venue-below-modules": VENUE_BELOW_MODULES_MAX_IGNORES,
    "runtime-not-research": RUNTIME_NOT_RESEARCH_MAX_IGNORES,
    "modules-acyclic": MODULES_ACYCLIC_MAX_IGNORES,
    "strategy-is-pure": STRATEGY_IS_PURE_MAX_IGNORES,
    "strategy-lower-only": STRATEGY_LOWER_ONLY_MAX_IGNORES,
    "strategy-no-internal-access": STRATEGY_NO_INTERNAL_ACCESS_MAX_IGNORES,
}


def test_import_contracts_keep_ignore_ratchet() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as config_file:
        contracts = tomllib.load(config_file)["tool"]["importlinter"]["contracts"]

    contracts_by_id = {contract["id"]: contract for contract in contracts}
    assert MAX_IGNORES_BY_ID.keys() <= contracts_by_id.keys()
    for contract_id, max_ignores in MAX_IGNORES_BY_ID.items():
        assert len(contracts_by_id[contract_id].get("ignore_imports", [])) <= max_ignores

    modules_dir = pyproject.parent / "src" / "bfx_funding_bot" / "modules"
    sibling_modules = {
        f"bfx_funding_bot.modules.{path.name}"
        for path in modules_dir.iterdir()
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and path.name not in {"candles", "strategy"}
    }
    assert set(contracts_by_id["strategy-lower-only"]["forbidden_modules"]) == sibling_modules
    shadow_sources = set(contracts_by_id["trading-shadow-no-internal-access"]["source_modules"])
    assert shadow_sources == (sibling_modules | {
        "bfx_funding_bot.modules.strategy", "bfx_funding_bot.modules.candles",
        "bfx_funding_bot.core", "bfx_funding_bot.external", "bfx_funding_bot.apps",
    }) - {"bfx_funding_bot.modules.trading_shadow"}
