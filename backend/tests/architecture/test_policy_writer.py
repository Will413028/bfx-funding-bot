"""One writer of the policy tables: no module but the ledger's policy store builds a row.

Mutation check: construct a policy revision or head row anywhere else in ``src``.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bfx_funding_bot"
ROW = re.compile(r"\b(CapitalPolicyRevisionRow|CapitalPolicyHeadRow)\(")


def test_only_the_policy_store_builds_policy_rows() -> None:
    builders = sorted(
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
        for line in path.read_text().splitlines()
        if ROW.search(line) and not line.lstrip().startswith("class ")
    )
    assert set(builders) == {"modules/ledger/_internal/policy_store.py"}
