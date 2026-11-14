"""Check the carry-over inventory of the cell-limit plan.

Every row has every column filled, a disposition that starts with a known verb, and a
step that exists in the plan. Every tracked source file that names one of the changed
symbols is covered by some row's location.

    python3 docs/plans/2026-11-14-cell-limit-resting-offers/check_inventory.py
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
COLUMNS = ("id", "位置", "現況", "去向", "步驟")
VERBS = ("保留", "改寫", "刪除")
STEPS = {"1", "2", "3", "4", "5", "6"}
PATTERN = (
    r"cell_exposure|max_cell_fraction|cell_limit|cell_headroom"
    r"|accepted_capital_basis_(credit_)?cell|AcceptedCapitalBasis(Credit)?Cell"
    r"|unattributed_credit|unattributed_lent|unattributedLent|attribution_basis"
    r"|trades_range_uncovered|credit_provenance_conflict"
)
ROOTS = (
    "backend/src", "backend/scripts", "backend/tests", "deploy",
    "frontend/src", "frontend/messages", "frontend/e2e",
)
# Named by the pattern for an unrelated reason: a count of unattributed credits in reports.
NOT_IN_SCOPE = (
    "backend/src/bfx_funding_bot/modules/live_validation/",
    "backend/tests/modules/live_validation/",
)


def main() -> int:
    with (HERE / "inventory.tsv").open(newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    errors: list[str] = []
    ids: set[str] = set()
    for number, row in enumerate(rows, 2):
        if tuple(row) != COLUMNS:
            errors.append(f"line {number}: columns are {tuple(row)}")
            continue
        for column in COLUMNS:
            if not (row[column] or "").strip():
                errors.append(f"line {number}: empty {column}")
        if row["id"] in ids:
            errors.append(f"line {number}: duplicate id {row['id']}")
        ids.add(row["id"])
        if not row["去向"].startswith(VERBS):
            errors.append(f"line {number}: disposition must start with one of {VERBS}")
        if not set(row["步驟"].split(",")) <= STEPS:
            errors.append(f"line {number}: unknown step {row['步驟']}")
    locations = "\n".join(row["位置"] for row in rows)
    hits = subprocess.run(
        ["git", "-C", str(REPO), "grep", "-lE", PATTERN, "--", *ROOTS],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    for path in hits:
        if path.startswith(NOT_IN_SCOPE):
            continue
        if path not in locations:
            errors.append(f"{path}: names a changed symbol but no inventory row covers it")
    for error in errors:
        print(error)
    print(f"{len(rows)} rows, {len(hits)} files named by the pattern, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
