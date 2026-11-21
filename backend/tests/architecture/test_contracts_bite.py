"""Every import-linter contract reports a violation planted in a copy of the real graph.

import-linter stays green when a contract matches nothing (a wrong module expression, a
renamed package), so a passing ``lint-imports`` alone does not prove a rule guards
anything. For each contract in ``pyproject.toml`` this builds the contract from its own
options, checks it is kept on the real graph, then adds one import that breaks it (source
-> forbidden module for every pair of entries, plus a two-hop chain when indirect imports
are forbidden; a two-way import between siblings; an unlisted importer of each protected
module) and expects every one to fail the check.
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
HOP = f"{ROOT_PACKAGE}.planted_hop"  # a module no contract names: an indirect chain's middle
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


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def _first(graph: grimp.ImportGraph, contract: Any, field: str, raw: str) -> str:
    """The first module one raw entry of a contract field resolves to."""
    entry = next(e for e in getattr(contract, field) if str(e) == raw or e.expression == raw)
    modules = _modules(graph, [entry])
    assert modules, f"{field} entry {raw!r} matches no module"
    return modules[0]


def _violations(graph: grimp.ImportGraph, options: dict[str, Any]
                ) -> list[list[tuple[str, str]]]:
    """Each element is one planted violation (a list of imports to add together)."""
    kind = options["type"]
    contract: Any = _build(options)
    if kind == "forbidden":
        out = []
        for source_raw in options["source_modules"]:
            for forbidden_raw in options["forbidden_modules"]:
                source = _first(graph, contract, "source_modules", source_raw)
                forbidden = _first(graph, contract, "forbidden_modules", forbidden_raw)
                if _overlaps(source, forbidden):
                    continue
                out.append([(source, forbidden)])
                if not options.get("allow_indirect_imports", False):
                    out.append([(source, HOP), (HOP, forbidden)])
        return out
    if kind == "acyclic_siblings":
        children = sorted(graph.find_children(options["ancestors"][0]))
        assert len(children) >= 2, "an acyclic contract over fewer than two siblings guards nothing"
        return [[(children[0], children[1]), (children[1], children[0])]]
    if kind == "protected":
        allowed = _modules(graph, contract.allowed_importers)
        out = []
        for protected_raw in options["protected_modules"]:
            protected = _first(graph, contract, "protected_modules", protected_raw)
            importer = next((m for m in sorted(graph.modules)
                             if m.startswith(ROOT_PACKAGE + ".") and m not in allowed
                             and not _overlaps(m, protected)), None)
            assert importer is not None, f"no unlisted importer found for {protected}"
            out.append([(importer, protected)])
        return out
    raise AssertionError(f"no planted violation for contract type {kind!r}")


def test_every_contract_type_is_covered() -> None:
    assert {c["type"] for c in _contracts()} <= CONTRACT_TYPES.keys()


@pytest.mark.parametrize("options", _contracts(), ids=lambda c: c["id"])
def test_contract_reports_a_planted_violation(options: dict[str, Any]) -> None:
    graph = _graph()  # contracts may mutate the graph they check, so each check gets a copy
    assert _build(options).check(copy.deepcopy(graph), verbose=False).kept, (
        "contract is broken on the real graph")
    violations = _violations(graph, options)
    assert violations
    missed = []
    for imports in violations:
        planted = copy.deepcopy(graph)
        for importer, imported in imports:
            planted.add_import(importer=importer, imported=imported, line_number=1,
                               line_contents="# planted violation")
        if _build(options).check(planted, verbose=False).kept:
            missed.append(imports)
    assert missed == []
