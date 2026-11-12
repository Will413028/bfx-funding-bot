"""The advisory-lock keys are part of the deployed contract, not an implementation detail.

Every writer of one account (the daemon, the operator CLI, the owner's one-shot scripts)
serialises against the others only if they derive the same key for that account. The
keys are the namespace and the account alone: a database holds one realm, so the
deployment environment is not part of them (they changed once, in the release that
dropped the per-row column; a version on the other side of that release cannot write,
see ``tests/integration/test_drop_realm_columns.py``). Literal values pin both formulas.

Mutation: change a namespace string or the digest size, or put the environment back
into either formula, and these fail.
"""
from __future__ import annotations

import inspect

from bfx_funding_bot.core.writer_lock import derive_lock_key
from bfx_funding_bot.core.xact_lock import acquire_transaction_lock, derive_transaction_lock_key

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"


def test_the_daemon_session_lock_key_is_stable() -> None:
    assert derive_lock_key(ACCOUNT) == -4521922529897354588


def test_the_scope_transaction_lock_key_is_stable() -> None:
    assert derive_transaction_lock_key(ACCOUNT) == -4686754208017115891


def test_two_accounts_never_share_a_key() -> None:
    assert derive_lock_key(ACCOUNT) != derive_lock_key(OTHER)
    assert derive_transaction_lock_key(ACCOUNT) != derive_transaction_lock_key(OTHER)
    assert derive_lock_key(ACCOUNT) != derive_transaction_lock_key(ACCOUNT)


def test_the_keys_take_the_account_and_nothing_else() -> None:
    assert list(inspect.signature(derive_lock_key).parameters) == ["account_id"]
    assert list(inspect.signature(derive_transaction_lock_key).parameters) == ["account_id"]
    assert list(inspect.signature(acquire_transaction_lock).parameters) == ["session", "account_id"]
