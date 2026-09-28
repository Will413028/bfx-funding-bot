"""Ratchet import exceptions: lower a constant when ignores are removed.

Never raise a constant to make a new violation pass silently.
"""

import tomllib
from pathlib import Path

CORE_IS_LEAF_MAX_IGNORES = 1
VENUE_BELOW_MODULES_MAX_IGNORES = 24
RUNTIME_NOT_RESEARCH_MAX_IGNORES = 5
MODULES_ACYCLIC_MAX_IGNORES = 61
APPS_IS_TOP_MAX_IGNORES = 0

MAX_IGNORES_BY_ID = {
    "apps-is-top": APPS_IS_TOP_MAX_IGNORES,
    "core-is-leaf": CORE_IS_LEAF_MAX_IGNORES,
    "venue-below-modules": VENUE_BELOW_MODULES_MAX_IGNORES,
    "runtime-not-research": RUNTIME_NOT_RESEARCH_MAX_IGNORES,
    "modules-acyclic": MODULES_ACYCLIC_MAX_IGNORES,
}


def test_import_contracts_keep_ignore_ratchet() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as config_file:
        contracts = tomllib.load(config_file)["tool"]["importlinter"]["contracts"]

    contracts_by_id = {contract["id"]: contract for contract in contracts}
    assert MAX_IGNORES_BY_ID.keys() <= contracts_by_id.keys()
    for contract_id, max_ignores in MAX_IGNORES_BY_ID.items():
        assert len(contracts_by_id[contract_id].get("ignore_imports", [])) <= max_ignores
