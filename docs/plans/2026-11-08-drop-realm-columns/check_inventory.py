#!/usr/bin/env python3
"""Fail if inventory.tsv has a blank 去向 (or any blank field), or a live table lacks its rows.

Usage: python3 docs/plans/2026-11-08-drop-realm-columns/check_inventory.py [inventory.tsv]
"""
import sys
from collections import defaultdict
from pathlib import Path

ARCHIVE = {"legacy_archive", "release_archive"}


def main(path: Path) -> int:
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    header, rows = lines[0].split("\t"), [l.split("\t") for l in lines[1:]]
    errors = []
    kinds = defaultdict(set)
    for n, row in enumerate(rows, start=2):
        if len(row) != len(header) or any(not cell.strip() for cell in row):
            errors.append(f"row {n}: blank or missing field: {row[:4]}")
            continue
        kinds[(row[0], row[1])].add(row[2])
    for (schema, table), ks in sorted(kinds.items()):
        if "column" not in ks:
            errors.append(f"{schema}.{table}: no column row")
        if schema not in ARCHIVE and "trigger" not in ks:
            errors.append(f"{schema}.{table}: live table without realm trigger row")
    live = sum(1 for (s, _) in kinds if s not in ARCHIVE)
    for e in errors:
        print("FAIL", e)
    print(f"{len(rows)} rows, {len(kinds)} tables ({live} live), {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    default = Path(__file__).with_name("inventory.tsv")
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else default))
