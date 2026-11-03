"""Ratchet import exceptions: each constant equals the current ignore count.

Removing an ignore fails until its constant is lowered to match; never raise a
constant to make a new violation pass silently.
"""

import ast
import re
import tomllib
from pathlib import Path

CORE_IS_LEAF_MAX_IGNORES = 0
VENUE_BELOW_MODULES_MAX_IGNORES = 0
RUNTIME_NOT_RESEARCH_MAX_IGNORES = 0
MODULES_ACYCLIC_MAX_IGNORES = 0
APPS_IS_TOP_MAX_IGNORES = 0
STRATEGY_IS_PURE_MAX_IGNORES = 0
STRATEGY_LOWER_ONLY_MAX_IGNORES = 0
STRATEGY_NO_INTERNAL_ACCESS_MAX_IGNORES = 0
TRADING_IS_PURE_MAX_IGNORES = 0

MAX_IGNORES_BY_ID = {
    "ledger-no-internal-access": 0,
    "ledger-wiring-is-top": 0,
    "ledger-not-legacy": 0,
    "simulated-venue-independent": 0,
    "simulated-venue-no-internal-access": 0,
    "simulated-venue-wiring-is-top": 0,
    "runtime-not-simulated-venue": 0,
    "trading-not-ledger": 0,
    "control-plane-exposure": 0,
    "xact-lock-owner": 0,
    "execution-acyclic": 0,
    "market-contracts-are-pure": 0,
    "performance-is-pure": 0,
    "market-contracts-no-sibling-dependencies": 0,
    "market-contracts-via-facade": 0,
    "strategy-wiring-is-top": 0,
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
    assert MAX_IGNORES_BY_ID.keys() == contracts_by_id.keys()
    for contract_id, max_ignores in MAX_IGNORES_BY_ID.items():
        assert len(contracts_by_id[contract_id].get("ignore_imports", [])) == max_ignores

    modules_dir = pyproject.parent / "src" / "bfx_funding_bot" / "modules"
    sibling_modules = {
        f"bfx_funding_bot.modules.{path.name}"
        for path in modules_dir.iterdir()
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and path.name not in {"candles", "strategy"}
    }
    assert set(contracts_by_id["strategy-lower-only"]["forbidden_modules"]) == sibling_modules

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
        "bfx_funding_bot.modules.market.book_period_coverage",
        "bfx_funding_bot.modules.market.contracts",
    }
    for contract_id in (
        "market-contracts-are-pure",
        "market-contracts-no-sibling-dependencies",
    ):
        assert set(contracts_by_id[contract_id]["source_modules"]) == market_sources
        assert contracts_by_id[contract_id]["as_packages"] is False
    assert contracts_by_id["performance-is-pure"]["source_modules"] == [
        "bfx_funding_bot.modules.performance",
    ]
    assert set(contracts_by_id["performance-is-pure"]["forbidden_modules"]) == {
        "sqlalchemy",
        "httpx",
        "asyncpg",
        "yaml",
        "bfx_funding_bot.core.settings",
    }
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
    simulated_internal = contracts_by_id["simulated-venue-no-internal-access"]
    simulated = "bfx_funding_bot.modules.simulated_venue"
    assert set(simulated_internal["source_modules"]) == (sibling_modules | {
        "bfx_funding_bot.modules.strategy", "bfx_funding_bot.modules.candles",
        "bfx_funding_bot.core", "bfx_funding_bot.core.**",
        "bfx_funding_bot.external", "bfx_funding_bot.external.**",
        "bfx_funding_bot.apps", "bfx_funding_bot.apps.**",
    }) - {simulated}
    assert simulated_internal["forbidden_modules"] == [f"{simulated}._internal"]
    assert simulated_internal["as_packages"] is True
    simulated_wiring = contracts_by_id["simulated-venue-wiring-is-top"]
    assert simulated_wiring["source_modules"] == wiring["source_modules"]
    assert simulated_wiring["forbidden_modules"] == [f"{simulated}.wiring"]
    assert simulated_wiring["as_packages"] is False
    not_imported = contracts_by_id["runtime-not-simulated-venue"]
    assert set(not_imported["source_modules"]) == set(simulated_internal["source_modules"]) - {
        "bfx_funding_bot.apps", "bfx_funding_bot.apps.**",
    }
    assert not_imported["forbidden_modules"] == [simulated]
    independent = contracts_by_id["simulated-venue-independent"]
    assert independent["source_modules"] == [simulated]
    assert {
        "bfx_funding_bot.modules.ledger", "bfx_funding_bot.modules.execution",
        "bfx_funding_bot.modules.trading", "bfx_funding_bot.modules.marketfeed",
        "bfx_funding_bot.apps",
    } <= set(independent["forbidden_modules"])


def test_contract_modules_exist() -> None:
    """import-linter keeps a contract naming a module that does not exist: a forbidden entry
    whose module was renamed or moved would pass silently. Every in-package module any
    contract field names (a wildcard entry: the part before its first wildcard segment)
    must exist, so the PR that moves a module also moves its entries."""
    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as config_file:
        contracts = tomllib.load(config_file)["tool"]["importlinter"]["contracts"]

    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for element in value for item in strings(element)]
        return []

    missing: list[str] = []
    for contract in contracts:
        for value in contract.values():
            for text in strings(value):
                for module in re.findall(r"bfx_funding_bot[\w.*]*", text):
                    segments = module.split(".")
                    if any("*" in segment for segment in segments):
                        segments = segments[: next(i for i, seg in enumerate(segments) if "*" in seg)]
                    path = root / "src" / "/".join(segments)
                    if not (path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()):
                        missing.append(f"{contract['id']}: {module}")
    assert missing == []


# What a script (``backend/scripts``, a composition root outside the import-linter root
# package until R11 adds it) must not import: a module's wiring, which only apps assemble
# (brief D-F ``wiring-is-top``), and the scope lock primitive, taken only through the
# ledger's ``ScopeLock`` (``xact-lock-owner``, ADR 2026-10-19 D4). Delete this test when
# ``scripts`` becomes a root package and the contracts see it.
SCRIPTS_MAY_NOT_IMPORT = re.compile(
    r"bfx_funding_bot\.(modules\.\w+\.wiring|core\.xact_lock)(\..*)?"
)


def test_scripts_import_neither_module_wiring_nor_the_lock_primitive() -> None:
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    violations: list[str] = []
    for script in scripts_dir.rglob("*.py"):
        tree = ast.parse(script.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            violations += [
                f"{script.relative_to(scripts_dir)}: {name}"
                for name in names if SCRIPTS_MAY_NOT_IMPORT.fullmatch(name)
            ]
    assert violations == []
