"""Every import-linter contract reports a violation planted in a copy of the real graph.

import-linter stays green when a contract matches nothing (a wrong module expression, a
renamed package), so a passing ``lint-imports`` alone does not prove a rule guards
anything. For each contract in ``pyproject.toml`` this builds the contract from its own
options, checks it is kept on the real graph, then adds one import that breaks it (source
-> forbidden module, a two-way import between siblings, an unlisted importer of a
protected module) and expects the check to fail.
"""
from __future__ import annotations

import copy
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

import grimp
import pytest
from importlinter.configuration import configure
from importlinter.contracts.acyclic_siblings import AcyclicSiblingsContract
from importlinter.contracts.forbidden import ForbiddenContract
from importlinter.contracts.protected import ProtectedContract
from importlinter.domain.contract import Contract
from importlinter.domain.helpers import module_expressions_to_modules

ROOT_PACKAGE = "bfx_funding_bot"
CONTRACT_TYPES: dict[str, type[Contract]] = {
    "forbidden": ForbiddenContract,
    "acyclic_siblings": AcyclicSiblingsContract,
    "protected": ProtectedContract,
}
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
configure()  # contract checks read import-linter's settings (timer, printer)


def _contracts() -> list[dict[str, Any]]:
    with _PYPROJECT.open("rb") as config_file:
        contracts: list[dict[str, Any]] = tomllib.load(config_file)["tool"]["importlinter"][
            "contracts"]
    return contracts


@cache
def _graph() -> grimp.ImportGraph:
    return grimp.build_graph(ROOT_PACKAGE, cache_dir=None, include_external_packages=True)


def _build(options: dict[str, Any]) -> Contract:
    # Field parsers take the raw strings an ini file would give, so booleans go as text.
    contract_options = {k: (str(v).lower() if isinstance(v, bool) else v)
                        for k, v in options.items() if k not in {"id", "name", "type"}}
    return CONTRACT_TYPES[options["type"]](
        name=options["name"],
        session_options={"root_packages": [ROOT_PACKAGE], "include_external_packages": "true"},
        contract_options=contract_options)


def _modules(graph: grimp.ImportGraph, expressions: Any) -> list[str]:
    return sorted(m.name for m in module_expressions_to_modules(graph, expressions))


def _violation(graph: grimp.ImportGraph, options: dict[str, Any]) -> list[tuple[str, str]]:
    kind = options["type"]
    contract: Any = _build(options)
    if kind == "forbidden":
        forbidden = _modules(graph, contract.forbidden_modules)
        sources = [m for m in _modules(graph, contract.source_modules)
                   if not any(m == f or m.startswith(f + ".") or f.startswith(m + ".")
                              for f in forbidden)]
        return [(sources[0], forbidden[0])]
    if kind == "acyclic_siblings":
        children = sorted(graph.find_children(options["ancestors"][0]))
        assert len(children) >= 2, "an acyclic contract over fewer than two siblings guards nothing"
        first, second = children[:2]
        return [(first, second), (second, first)]
    if kind == "protected":
        allowed = _modules(graph, contract.allowed_importers)
        protected = _modules(graph, contract.protected_modules)[0]
        importer = next(m for m in sorted(graph.modules)
                        if m not in allowed and not m.startswith(protected)
                        and m.count(".") == 2)
        return [(importer, protected)]
    raise AssertionError(f"no planted violation for contract type {kind!r}")


def test_every_contract_type_is_covered() -> None:
    assert {c["type"] for c in _contracts()} <= CONTRACT_TYPES.keys()


@pytest.mark.parametrize("options", _contracts(), ids=lambda c: c["id"])
def test_contract_reports_a_planted_violation(options: dict[str, Any]) -> None:
    graph = _graph()
    assert _build(options).check(graph, verbose=False).kept, "contract is broken on the real graph"
    planted = copy.deepcopy(graph)
    for importer, imported in _violation(planted, options):
        planted.add_import(importer=importer, imported=imported, line_number=1,
                           line_contents="# planted violation")
    assert not _build(options).check(planted, verbose=False).kept
