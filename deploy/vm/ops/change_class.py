"""Classify a release as `standard` or `material` from the paths it changes.

Host-side and standard-library only (the VM runs Ubuntu 24.04's python3.12).
The rules come from `deploy/change-class.yaml` at the *target* commit, so a
change to the rules is itself judged by the rules it ships with -- which is why
the rules file is hard-wired material here and cannot be listed away.

A path is `standard` only if a `standard` pattern matches it and no `material`
pattern does. One material path makes the release material. Anything that
cannot be decided (missing or malformed rules, unknown previous revision, a
failed diff) is material: the class can only ever be raised by uncertainty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

STANDARD = "standard"
MATERIAL = "material"
RULES_PATH = "deploy/change-class.yaml"

_PATTERN_CHARS = re.compile(r"[A-Za-z0-9._/*?+@-]+")
_ITEM = re.compile(r'-\s+(?:"([^"]*)"|(\S+))')
_KEY = re.compile(r"([a-z_]+):(.*)")
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


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Translate a repository glob into an anchored regular expression.

    `*` and `?` never cross `/`; `**/` spans zero or more whole directories and a
    trailing `**` spans everything below. fnmatch is not used because its `*`
    crosses directory separators, which would silently widen `standard`.
    """
    if (
        not pattern
        or _PATTERN_CHARS.fullmatch(pattern) is None
        or pattern.startswith("/")
        or "//" in pattern
        or any(part in {".", ".."} for part in pattern.split("/"))
    ):
        raise RulesError(f"invalid pattern: {pattern!r}")
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            at_segment_start = index == 0 or pattern[index - 1] == "/"
            rest = pattern[index + 2:]
            if not at_segment_start or (rest and not rest.startswith("/")):
                raise RulesError(f"'**' must be a whole path segment: {pattern!r}")
            if rest:
                out.append("(?:[^/]+/)*")
                index += 3
            else:
                out.append(".+")
                index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def parse_rules(text: str) -> Rules:
    """Parse the strict YAML subset used by deploy/change-class.yaml.

    Accepted shape: comment lines, `version: 1`, and the two block lists
    `standard:` and `material:` whose items are `- pattern` or `- "pattern"`.
    Anything else is an error so a typo can never widen `standard`.
    """
    sections: dict[str, list[str]] = {}
    version: str | None = None
    current: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            match = _KEY.fullmatch(line)
            if match is None:
                raise RulesError(f"line {number}: expected 'key:'")
            key, value = match.group(1), match.group(2).strip()
            if key in sections or (key == "version" and version is not None):
                raise RulesError(f"line {number}: duplicate key {key!r}")
            if key == "version":
                version = value
                current = None
            elif key in {"standard", "material"}:
                if value not in {"", "[]"}:
                    raise RulesError(f"line {number}: {key} must be a block list")
                sections[key] = []
                current = None if value == "[]" else key
            else:
                raise RulesError(f"line {number}: unknown key {key!r}")
            continue
        item = _ITEM.fullmatch(line.strip())
        if current is None or item is None:
            raise RulesError(f"line {number}: expected a '- pattern' list item")
        pattern = item.group(1) if item.group(1) is not None else item.group(2)
        compile_pattern(pattern)
        sections[current].append(pattern)
    if version != "1":
        raise RulesError("version must be 1")
    if set(sections) != {"standard", "material"}:
        raise RulesError("both 'standard' and 'material' are required")
    return Rules(standard=tuple(sections["standard"]), material=tuple(sections["material"]))


def path_class(path: str, rules: Rules) -> str:
    if path == RULES_PATH:
        return MATERIAL
    if any(compile_pattern(pattern).match(path) for pattern in rules.material):
        return MATERIAL
    if any(compile_pattern(pattern).match(path) for pattern in rules.standard):
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
