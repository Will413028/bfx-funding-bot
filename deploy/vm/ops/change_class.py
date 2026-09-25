"""Classify a release as `standard` or `material` from the paths it changes.

The rules come from `deploy/change-class.yaml` at the *target* commit, so a
change to the rules is itself judged by the rules it ships with -- which is why
the rules file is hard-wired material here and cannot be listed away.

A path is `standard` only if a `standard` pattern matches it and no `material`
pattern does. One material path makes the release material. Anything that
cannot be decided (missing or malformed rules, unknown previous revision, a
failed diff) is material: the class can only ever be raised by uncertainty.

Parsing is PyYAML (`safe_load`) and matching is `pathspec`'s gitignore
patterns, both pinned in deploy/vm/ops/uv.lock. Every pattern is anchored at
the repository root (a leading `/` is added), so `*` and `?` stay inside one
path segment, `**/` spans directories and a trailing `**` spans everything
below -- and a slash-less pattern such as `.gitignore` never floats to other
directories the way it would in a .gitignore file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import pathspec
import yaml

STANDARD = "standard"
MATERIAL = "material"
RULES_PATH = "deploy/change-class.yaml"

# Negation (`!`), character classes and escapes are gitignore features that
# could silently widen `standard`; the rules never need them.
_PATTERN_CHARS = re.compile(r"[A-Za-z0-9._/*?+@-]+")
_MAX_REPORTED_PATHS = 20


class RulesError(ValueError):
    """The rules file is missing, malformed or contains an unsafe pattern."""


@dataclass(frozen=True, slots=True)
class Rules:
    standard: tuple[str, ...]
    material: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Classification:
    change_class: str
    reason: str
    material_paths: tuple[str, ...] = ()

    def describe(self) -> str:
        if not self.material_paths:
            return self.reason
        shown = ",".join(self.material_paths[:_MAX_REPORTED_PATHS])
        more = len(self.material_paths) - _MAX_REPORTED_PATHS
        return f"{self.reason}:{shown}" + (f",+{more}" if more > 0 else "")


def validate_pattern(pattern: object) -> str:
    if (
        not isinstance(pattern, str)
        or not pattern
        or _PATTERN_CHARS.fullmatch(pattern) is None
        or pattern.startswith("/")
        or pattern.endswith("/")
        or "//" in pattern
        or any(part in {".", ".."} for part in pattern.split("/"))
        or any("**" in part and part != "**" for part in pattern.split("/"))
    ):
        raise RulesError(f"invalid pattern: {pattern!r}")
    return pattern


@lru_cache(maxsize=256)
def _spec(patterns: tuple[str, ...]) -> pathspec.PathSpec[Any]:
    return pathspec.PathSpec.from_lines("gitignore", ["/" + validate_pattern(p) for p in patterns])


def matches(patterns: Sequence[str], path: str) -> bool:
    """Whether any repository-anchored pattern matches `path`."""
    return bool(patterns) and _spec(tuple(patterns)).match_file(path)


class _UniqueKeyLoader(yaml.SafeLoader):
    """safe_load, except a repeated key is an error instead of last-one-wins."""


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    keys = [loader.construct_object(key, deep=deep) for key, _ in node.value]
    if len(keys) != len(set(map(repr, keys))):
        raise yaml.constructor.ConstructorError(None, None, "duplicate key", node.start_mark)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def parse_rules(text: str) -> Rules:
    """Read `version: 1` plus the `standard` and `material` pattern lists; nothing else."""
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise RulesError(f"not YAML: {type(exc).__name__}") from None
    if not isinstance(data, dict) or set(data) != {"version", "standard", "material"}:
        raise RulesError("expected exactly the keys version, standard and material")
    if data["version"] != 1 or isinstance(data["version"], bool):
        raise RulesError("version must be 1")
    lists: dict[str, tuple[str, ...]] = {}
    for key in ("standard", "material"):
        value = data[key] if data[key] is not None else []
        if not isinstance(value, list):
            raise RulesError(f"{key} must be a list of patterns")
        lists[key] = tuple(validate_pattern(item) for item in value)
    return Rules(standard=lists["standard"], material=lists["material"])


def path_class(path: str, rules: Rules) -> str:
    if path == RULES_PATH or matches(rules.material, path):
        return MATERIAL
    if matches(rules.standard, path):
        return STANDARD
    return MATERIAL


def classify(paths: Iterable[str], rules: Rules) -> Classification:
    changed = sorted(set(paths))
    material = tuple(path for path in changed if path_class(path, rules) == MATERIAL)
    if material:
        return Classification(MATERIAL, "material_paths", material)
    if not changed:
        return Classification(STANDARD, "no_path_changes")
    return Classification(STANDARD, "standard_paths_only")


def undecidable(reason: str) -> Classification:
    """Every path the classifier cannot judge ends here, never at `standard`."""
    return Classification(MATERIAL, reason)


def raise_to_material(result: Classification, reason: str) -> Classification:
    if result.change_class == MATERIAL:
        return result
    return Classification(MATERIAL, reason)
