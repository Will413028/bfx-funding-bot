"""The advisory-lock keys are part of the deployed contract, not an implementation detail.

During a release the old and the new image (and the owner's one-shot scripts, which run
whatever image is deployed) serialise against each other only if they derive the same
key for the same account and environment. Literal values pin both formulas.

Mutation: change a namespace string or the digest size and these fail.
"""
from __future__ import annotations

from bfx_funding_bot.core.writer_lock import derive_lock_key
from bfx_funding_bot.core.xact_lock import derive_transaction_lock_key

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def test_the_daemon_session_lock_key_is_stable() -> None:
    assert derive_lock_key(ACCOUNT, "prod") == 6093535492373611659


def test_the_scope_transaction_lock_key_is_stable() -> None:
    assert derive_transaction_lock_key(ACCOUNT, "prod") == 1966872880379107853
    assert derive_transaction_lock_key(ACCOUNT, "shadow") == 7929136968680488685
