"""Ratchet import exceptions: lower a constant when ignores are removed.

Never raise a constant to make a new violation pass silently.
"""

import ast
import tomllib
from pathlib import Path

CORE_IS_LEAF_MAX_IGNORES = 0
VENUE_BELOW_MODULES_MAX_IGNORES = 19
RUNTIME_NOT_RESEARCH_MAX_IGNORES = 0
MODULES_ACYCLIC_MAX_IGNORES = 28
APPS_IS_TOP_MAX_IGNORES = 0
STRATEGY_IS_PURE_MAX_IGNORES = 0
STRATEGY_LOWER_ONLY_MAX_IGNORES = 0
STRATEGY_NO_INTERNAL_ACCESS_MAX_IGNORES = 0
TRADING_IS_PURE_MAX_IGNORES = 0

MAX_IGNORES_BY_ID = {
    "capital-consumers-via-ports": 0,
    "planner-via-ports": 0,
    "ledger-no-internal-access": 0,
    "ledger-wiring-is-top": 0,
    "ledger-not-legacy": 0,
    "trading-not-ledger": 0,
    "market-contracts-are-pure": 0,
    "market-contracts-no-sibling-dependencies": 0,
    "market-contracts-via-facade": 0,
    "capital-comparison-no-settings": 0,
    "strategy-wiring-is-top": 0,
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

    strategy_sources = set(contracts_by_id["strategy-no-internal-access"]["source_modules"])
    assert strategy_sources == sibling_modules | {
        "bfx_funding_bot.modules.candles", "bfx_funding_bot.core",
        "bfx_funding_bot.external", "bfx_funding_bot.apps",
    }
    market_siblings = (
        sibling_modules
        | {
            "bfx_funding_bot.modules.candles",
            "bfx_funding_bot.modules.strategy",
        }
    ) - {"bfx_funding_bot.modules.market"}
    market_sources = {
        "bfx_funding_bot.modules.market",
        "bfx_funding_bot.modules.market.contracts",
    }
    for contract_id in (
        "market-contracts-are-pure",
        "market-contracts-no-sibling-dependencies",
    ):
        assert set(contracts_by_id[contract_id]["source_modules"]) == market_sources
        assert contracts_by_id[contract_id]["as_packages"] is False
    assert set(contracts_by_id["market-contracts-are-pure"]["forbidden_modules"]) == {
        "sqlalchemy",
        "httpx",
        "asyncpg",
        "yaml",
        "bfx_funding_bot.core.settings",
    }
    assert set(
        contracts_by_id["market-contracts-no-sibling-dependencies"]["forbidden_modules"]
    ) == market_siblings | {f"{module}.**" for module in market_siblings}
    facade = contracts_by_id["market-contracts-via-facade"]
    assert set(facade["source_modules"]) == market_siblings | {
        "bfx_funding_bot.core",
        "bfx_funding_bot.external",
        "bfx_funding_bot.apps",
    }
    assert facade["forbidden_modules"] == ["bfx_funding_bot.modules.market.contracts"]
    assert facade["allow_indirect_imports"] is True
    assert set(contracts_by_id["runtime-not-research"]["source_modules"]) == (
        market_siblings | {"bfx_funding_bot.modules.market"}
    ) - {
        f"bfx_funding_bot.modules.{name}"
        for name in (
            "backfill",
            "backtest",
            "live_validation",
            "strategy",
            "trading",
            "trading_shadow",
        )
    }
    wiring = contracts_by_id["strategy-wiring-is-top"]
    assert set(wiring["source_modules"]) == {
        "bfx_funding_bot.core", "bfx_funding_bot.core.**",
        "bfx_funding_bot.external", "bfx_funding_bot.external.**",
        "bfx_funding_bot.modules", "bfx_funding_bot.modules.**",
    }
    assert wiring["forbidden_modules"] == ["bfx_funding_bot.modules.strategy.wiring"]
    assert wiring["as_packages"] is False
    ledger_wiring = contracts_by_id["ledger-wiring-is-top"]
    assert ledger_wiring["source_modules"] == wiring["source_modules"]
    assert ledger_wiring["forbidden_modules"] == ["bfx_funding_bot.modules.ledger.wiring"]
    assert ledger_wiring["as_packages"] is False
    ledger_internal = contracts_by_id["ledger-no-internal-access"]
    assert set(ledger_internal["source_modules"]) == (sibling_modules | {
        "bfx_funding_bot.modules.strategy", "bfx_funding_bot.modules.candles",
        "bfx_funding_bot.core", "bfx_funding_bot.core.**",
        "bfx_funding_bot.external", "bfx_funding_bot.external.**",
        "bfx_funding_bot.apps", "bfx_funding_bot.apps.**",
    }) - {"bfx_funding_bot.modules.ledger"}
    assert ledger_internal["forbidden_modules"] == ["bfx_funding_bot.modules.ledger._internal"]
    assert ledger_internal["as_packages"] is True
    assert ledger_internal["allow_indirect_imports"] is True
    legacy_authority = {
        f"bfx_funding_bot.modules.execution.{name}"
        for name in ("capital_repository", "capital_runtime", "event_store.tables",
                     "uncertainty_tables")
    }
    consumers = contracts_by_id["capital-consumers-via-ports"]
    assert set(consumers["source_modules"]) == {
        "bfx_funding_bot.modules.admin.trading_status",
        "bfx_funding_bot.modules.execution.command_gate",
        "bfx_funding_bot.modules.execution.contracts",
        "bfx_funding_bot.modules.execution.deployment.sizing",
        "bfx_funding_bot.modules.execution.managed_cancel",
        "bfx_funding_bot.modules.execution.middleware.reservation_emitting",
        "bfx_funding_bot.modules.execution.safety.hard_guards",
        "bfx_funding_bot.modules.execution.safety.kill_switch",
        "bfx_funding_bot.modules.execution.safety.pre_trade",
    }
    assert set(consumers["forbidden_modules"]) == legacy_authority | {
        "bfx_funding_bot.modules.execution.amount_fingerprint",
    }
    planner = contracts_by_id["planner-via-ports"]
    assert planner["source_modules"] == ["bfx_funding_bot.modules.execution.deployment.reconciler"]
    assert set(planner["forbidden_modules"]) == legacy_authority
    for contract in (consumers, planner):
        assert contract["type"] == "forbidden"
        assert contract["allow_indirect_imports"] is True


def test_research_scripts_obtain_strategy_wiring_through_apps() -> None:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    violations: list[str] = []
    for script in scripts_dir.rglob("*.py"):
        tree = ast.parse(script.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "bfx_funding_bot.modules.strategy.wiring"
            ):
                violations.append(str(script.relative_to(scripts_dir)))
            if isinstance(node, ast.Import) and any(
                alias.name == "bfx_funding_bot.modules.strategy.wiring"
                for alias in node.names
            ):
                violations.append(str(script.relative_to(scripts_dir)))
    assert violations == []
