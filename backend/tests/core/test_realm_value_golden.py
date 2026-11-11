"""Values derived from the deployment environment that outlive one process.

The HKDF info binds a sealed credential to its account, environment, recipient and
request: the operator's process seals with it and the bot opens with it, so both must
derive the same bytes from the same configured value. Literal values pin the format
(the advisory-lock keys are pinned the same way in tests/core/test_lock_keys.py).

Mutation: change ``INFO_LABEL``, the separator or the field order in
``SealContext.info`` and these fail.
"""
from __future__ import annotations

from uuid import UUID

import pytest

from bfx_funding_bot.core.sealed_credential import SealContext

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
RECIPIENT = UUID("11111111-2222-4333-8444-555555555555")
REQUEST = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")


@pytest.mark.parametrize(("environment", "info"), [
    ("prod", b"bfx-credential-rotation/v1\x00aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\x00prod"
             b"\x0011111111-2222-4333-8444-555555555555\x0066666666-7777-4888-8999-aaaaaaaaaaaa"),
    ("ci", b"bfx-credential-rotation/v1\x00aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\x00ci"
           b"\x0011111111-2222-4333-8444-555555555555\x0066666666-7777-4888-8999-aaaaaaaaaaaa"),
])
def test_the_seal_info_is_stable(environment: str, info: bytes) -> None:
    assert SealContext(ACCOUNT, environment, RECIPIENT, REQUEST).info() == info
