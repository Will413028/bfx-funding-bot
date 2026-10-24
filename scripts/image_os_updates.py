"""Fail when the released backend image is missing a Debian security update.

Reads `apt list --upgradable` (run inside the image after `apt-get update`) on stdin and
fails on every upgrade from a `*-security` suite that image-os-updates.toml does not list
with a future `ignoreUntil`. An expired entry also fails, so the baseline cannot outlive
its deadline. Upgrades from other suites (point releases, `-updates`) are not checked.

Why this and not an OSV gate: most OSV findings for the Debian base have no fix Debian
has shipped, so a deadline on them can never be met (measured 2026-10-24: 164 findings,
3 upgradable packages). What Debian has shipped is what we can install.

    python3 scripts/image_os_updates.py --baseline image-os-updates.toml < upgradable.txt
"""

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# `name/suite[,suite] version arch [upgradable from: old]`
LINE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<suites>\S+) (?P<version>\S+) \S+ \[upgradable from: (?P<old>[^\]]+)\]$"
)


@dataclass(frozen=True, slots=True)
class Upgrade:
    package: str
    version: str
    installed: str


def security_upgrades(text: str) -> list[Upgrade]:
    upgrades = []
    for line in text.splitlines():
        match = LINE.match(line.strip())
        if match and any(suite.endswith("-security") for suite in match["suites"].split(",")):
            upgrades.append(Upgrade(match["name"], match["version"], match["old"]))
    return upgrades


def problems(upgrades: list[Upgrade], baseline: dict[str, Any], now: datetime) -> list[str]:
    found = []
    listed = {}
    for entry in baseline.get("Pending", []):
        until = entry["ignoreUntil"]
        key = (entry["package"], entry["version"])
        if until <= now:
            found.append(
                f"{key[0]} {key[1]}: baseline entry expired {until:%Y-%m-%d} ({entry['reason']})"
            )
        else:
            listed[key] = until
    for upgrade in upgrades:
        if (upgrade.package, upgrade.version) not in listed:
            found.append(
                f"{upgrade.package}: security update {upgrade.version} not installed "
                f"(image has {upgrade.installed})"
            )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args(argv)
    upgrades = security_upgrades(sys.stdin.read())
    baseline = tomllib.loads(args.baseline.read_text(encoding="utf-8"))
    for upgrade in upgrades:
        print(f"pending: {upgrade.package} {upgrade.installed} -> {upgrade.version}")
    found = problems(upgrades, baseline, datetime.now(UTC))
    for problem in found:
        print(f"::error::{problem}")
    if not found:
        print(f"ok: {len(upgrades)} pending security update(s), all listed in {args.baseline.name}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
