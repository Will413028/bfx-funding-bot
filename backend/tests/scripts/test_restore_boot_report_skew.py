"""The boot report of two image generations, parsed by one drill (`ledger_digest.parse_boot`).

A release that carries 545b2877e9bd (the realm column leaves every row) is deployed by a drill of
the TARGET release, which parses two reports in one run: the verification phase runs the
DEPLOYED image (its report still names ``deployment_environment`` per scope), the rehearsal runs
the CANDIDATE image on the migrated copy (its report does not). A scope is the exchange account
alone in both.

Mutation: require ``deployment_environment`` in ``_BOOT_SCOPE_KEYS`` again:
``test_the_candidate_images_report_parses`` fails (restore_output_invalid).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
ACCOUNT = "3f19d046-5030-494c-9a0a-9573bb890c1f"
OTHER = "7c1e5f0a-2b3d-4e6f-8a9b-0c1d2e3f4a5b"
BASIS = "6a0c3c7e-0f43-4a8e-9b1f-1d7e2a3b4c5d"
DEPLOYED_HEAD = "96535191e443"  # the copy before the rehearsed migration
CANDIDATE_HEAD = "545b2877e9bd"

_spec = importlib.util.spec_from_file_location(
    "boot_report_skew_ledger_digest", ROOT / "deploy/vm/pgbackrest/ledger_digest.py")
assert _spec is not None and _spec.loader is not None
ledger = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ledger
_spec.loader.exec_module(ledger)

# What the image before this release prints (apps/restore_boot_check.py at 89b06dc4,
# json.dumps(sort_keys=True)): every scope names its realm.
DEPLOYED_REPORT = (
    '{"boot": {"authority": "ledger", "realm": "prod", "schema_head": "' + DEPLOYED_HEAD
    + '", "scopes": [{"basis_id": "' + BASIS + '", "deployment_environment": "prod", '
    '"exchange_account_id": "' + ACCOUNT + '", "reads": [{"basis_id": "' + BASIS + '", '
    '"cell_id": "c1", "result": "available", "symbol": "fUST"}]}]}}\n'
)
# What the candidate image prints: the same report without the per-scope realm.
CANDIDATE_REPORT = (
    '{"boot": {"authority": "ledger", "realm": "prod", "schema_head": "' + CANDIDATE_HEAD
    + '", "scopes": [{"basis_id": "' + BASIS + '", "exchange_account_id": "' + ACCOUNT + '", '
    '"reads": [{"basis_id": "' + BASIS + '", "cell_id": "c1", "result": "available", '
    '"symbol": "fUST"}]}]}}\n'
)


def _bounds(head: str, account: str = ACCOUNT) -> Any:
    lines = [f"schema\t180000\t{head}", *(f"table\t{name}" for name in ledger.LEDGER_TABLES),
             f"scope\t{account}\t5\t5\t4\t3\t1\t7", "epoch\t2\tledger",
             "inconsistent\tclock_behind\t0", "inconsistent\tvenue_offer_mirror_orphans\t0",
             "inconsistent\tvenue_credit_mirror_orphans\t0"]
    return ledger.parse_bounds("\n".join(lines) + "\n")


def test_the_deployed_images_report_parses() -> None:
    boot = ledger.parse_boot(DEPLOYED_REPORT, _bounds(DEPLOYED_HEAD))
    [scope] = boot["scopes"]
    assert (boot["realm"], scope["exchange_account_id"], scope["basis_id"]) == (
        "prod", ACCOUNT, BASIS)


def test_the_candidate_images_report_parses() -> None:
    boot = ledger.parse_boot(CANDIDATE_REPORT, _bounds(CANDIDATE_HEAD))
    [scope] = boot["scopes"]
    assert (boot["realm"], scope["exchange_account_id"], scope["basis_id"]) == (
        "prod", ACCOUNT, BASIS)


def test_both_generations_leave_the_same_receipt() -> None:
    deployed = ledger.parse_boot(DEPLOYED_REPORT, _bounds(DEPLOYED_HEAD))
    candidate = ledger.parse_boot(CANDIDATE_REPORT, _bounds(CANDIDATE_HEAD))
    assert deployed["scopes"] == candidate["scopes"]
    assert (deployed["schema_head"], candidate["schema_head"]) == (DEPLOYED_HEAD, CANDIDATE_HEAD)
    # The receipt keeps the keys the drill knows: the retired per-scope realm is not one.
    assert set(deployed["scopes"][0]) == {"exchange_account_id", "basis_id", "reads"}


@pytest.mark.parametrize(("report", "head"), [
    (DEPLOYED_REPORT, DEPLOYED_HEAD), (CANDIDATE_REPORT, CANDIDATE_HEAD),
], ids=["deployed", "candidate"])
def test_a_report_about_another_account_is_refused(report: str, head: str) -> None:
    with pytest.raises(ledger.LedgerVerificationError, match=r"^restore_output_invalid$"):
        ledger.parse_boot(report, _bounds(head, OTHER))


def test_each_report_is_bound_to_its_own_schema_head() -> None:
    # The deployed image cannot vouch for the migrated copy, nor the candidate for the old one.
    for report, head in ((DEPLOYED_REPORT, CANDIDATE_HEAD), (CANDIDATE_REPORT, DEPLOYED_HEAD)):
        with pytest.raises(ledger.LedgerVerificationError, match=r"^boot_schema_head_mismatch$"):
            ledger.parse_boot(report, _bounds(head))


def test_one_account_reported_under_two_realms_is_refused() -> None:
    # Only a deployed image could print this; an account is one scope, so it is a duplicate.
    payload = json.loads(DEPLOYED_REPORT)
    [scope] = payload["boot"]["scopes"]
    payload["boot"]["scopes"].append({**scope, "deployment_environment": "ci"})
    with pytest.raises(ledger.LedgerVerificationError, match=r"^restore_output_invalid$"):
        ledger.parse_boot(json.dumps(payload) + "\n", _bounds(DEPLOYED_HEAD))
