"""Release change class: standard only when every changed path is listed standard.

deploy/vm/ops/change_class.py is host-side standard-library code; the rules are
deploy/change-class.yaml. Default material, undecidable material, and the rules
file itself is always material.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RULES_TEXT = (ROOT / "deploy/change-class.yaml").read_text(encoding="utf-8")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cc = _load("vm_ops_change_class_under_test", ROOT / "deploy/vm/ops/change_class.py")
RULES = cc.parse_rules(RULES_TEXT)


def test_repository_rules_parse_exactly_as_a_real_yaml_parser_reads_them() -> None:
    """The strict subset parser must not silently disagree with YAML itself."""
    assert yaml.safe_load(RULES_TEXT) == {
        "version": 1, "standard": list(RULES.standard), "material": list(RULES.material),
    }


@pytest.mark.parametrize("paths", [
    ["docs/runbooks/offsite-dr.md"],
    ["README.md", "backend/ARCHITECTURE.md", "AGENTS.md"],
    ["frontend/src/features/dashboard/components/funding-panel.tsx"],
    ["backend/tests/scripts/test_vm_change_class.py", "backend/fixtures/candles/fUST_p2_1h.jsonl.gz"],
    ["backend/scripts/run_backtest.py", "backend/scripts/run_weekly_attribution.py"],
    ["infra/terraform/r2/main.tf", ".github/dependabot.yml", "lefthook.yml"],
])
def test_release_is_standard_only_when_every_path_is_standard(paths: list[str]) -> None:
    result = cc.classify(paths, RULES)
    assert (result.change_class, result.material_paths) == ("standard", ())


@pytest.mark.parametrize("path", [
    "backend/src/bfx_funding_bot/modules/execution/command_gate.py",
    "backend/src/bfx_funding_bot/modules/observability/metrics.py",
    "backend/configs/cells.live.yaml",
    "backend/configs/safety.live.yaml",
    "backend/alembic/versions/c3a639388457_add_deployments_ledger.py",
    "backend/alembic.ini",
    "backend/uv.lock",
    "backend/Dockerfile",
    "deploy/vm/live.env",
    "deploy/vm/docker-compose.app.yml",
    "deploy/change-class.yaml",
    "docker-compose.bot.yml",
    ".github/workflows/release.yml",
    "frontend/src/app/api/proxy/[...path]/route.ts",
    "frontend/src/lib/auth.ts",
    "frontend/middleware.ts",
    "frontend/scripts/bootstrap-operator.mjs",
    "backend/scripts/bootstrap_capital.py",
    "backend/scripts/verify_projection_replay.py",
    "backend/scripts/learn_book_fill_rate.py",
])
def test_material_path_makes_the_whole_release_material(path: str) -> None:
    result = cc.classify(["docs/notes.md", path], RULES)
    assert result.change_class == "material"
    assert result.material_paths == (path,)


@pytest.mark.parametrize("path", [
    "scripts/deploy-vm.sh",
    "backend/scripts/brand_new_tool.py",
    "some/new/top/level/file.py",
    "Makefile",
])
def test_unlisted_paths_default_to_material(path: str) -> None:
    assert cc.path_class(path, RULES) == "material"


def test_material_list_wins_over_a_matching_standard_glob() -> None:
    # `**/*.md` is standard, but nothing under backend/src or backend/configs is.
    for path in ("backend/src/bfx_funding_bot/NOTES.md", "backend/configs/README.md"):
        assert cc.path_class(path, RULES) == "material"


def test_rules_file_is_material_even_when_the_rules_say_everything_is_standard() -> None:
    permissive = cc.parse_rules('version: 1\nstandard:\n  - "**"\nmaterial: []\n')
    assert cc.path_class("backend/src/anything.py", permissive) == "standard"
    assert cc.path_class("deploy/change-class.yaml", permissive) == "material"


def test_no_changed_paths_is_standard() -> None:
    """Same commit rebuilt (e.g. a re-run release): nothing material changed."""
    assert cc.classify([], RULES).change_class == "standard"


def test_undecidable_and_raise_only_ever_produce_material() -> None:
    assert cc.undecidable("previous_revision_unknown").change_class == "material"
    standard = cc.classify(["docs/a.md"], RULES)
    raised = cc.raise_to_material(standard, "operator_forced")
    assert (raised.change_class, raised.reason) == ("material", "operator_forced")
    material = cc.classify(["backend/src/x.py"], RULES)
    assert cc.raise_to_material(material, "operator_forced") is material


@pytest.mark.parametrize(("pattern", "path", "expected"), [
    ("docs/**", "docs/a.md", True),
    ("docs/**", "docs/a/b/c.md", True),
    ("docs/**", "docsx/a.md", False),
    ("docs/**", "docs", False),
    ("**/*.md", "README.md", True),
    ("**/*.md", "a/b/c.md", True),
    ("**/*.md", "a/b/c.mdx", False),
    ("backend/scripts/*.py", "backend/scripts/x.py", True),
    ("backend/scripts/*.py", "backend/scripts/sub/x.py", False),
    ("docker-compose.*.yml", "docker-compose.bot.yml", True),
    ("docker-compose.*.yml", "deploy/docker-compose.bot.yml", False),
    ("a?c", "abc", True),
    ("a?c", "a/c", False),
    ("a/**/z", "a/z", True),
    ("a/**/z", "a/b/c/z", True),
    ("frontend/src/app/api/**", "frontend/src/app/api/auth/[...all]/route.ts", True),
])
def test_glob_segments_never_widen_silently(pattern: str, path: str, expected: bool) -> None:
    assert (cc.compile_pattern(pattern).match(path) is not None) is expected


@pytest.mark.parametrize("text", [
    "",
    "version: 2\nstandard: []\nmaterial: []\n",
    "version: 1\nstandard: []\n",
    "version: 1\nstandard:\n  - docs/**\nstandard:\n  - x\nmaterial: []\n",
    "version: 1\nstandard: frontend/**\nmaterial: []\n",
    "version: 1\nstandrd:\n  - docs/**\nmaterial: []\n",
    "version: 1\n  - docs/**\nstandard: []\nmaterial: []\n",
    "version: 1\nstandard:\n  - ../escape\nmaterial: []\n",
    "version: 1\nstandard:\n  - /absolute\nmaterial: []\n",
    "version: 1\nstandard:\n  - a**\nmaterial: []\n",
    "version: 1\nstandard:\n  - '**x/y'\nmaterial: []\n",
    "version: 1\nstandard:\n  - two words\nmaterial: []\n",
    "version: 1\nstandard:\n  - docs/[ab]/x\nmaterial: []\n",
])
def test_malformed_rules_are_rejected_not_guessed(text: str) -> None:
    with pytest.raises(cc.RulesError):
        cc.parse_rules(text)


def test_describe_bounds_the_reported_paths() -> None:
    result = cc.classify([f"backend/src/m{i}.py" for i in range(25)], RULES)
    described = result.describe()
    assert described.startswith("material_paths:backend/src/m0.py,")
    assert described.endswith(",+5")
