"""The bytes a sealed credential is bound to, pinned as literals.

The operator's process seals with ``SealContext.info`` and the bot opens with it, so
both must derive the same bytes. The info names the account, the recipient and the
request; it carries no deployment environment (a database holds one realm, and a sealed
request lives for one bot run).

Mutation: change ``INFO_LABEL``, the separator or the field order in
``SealContext.info``, or add a field back, and these fail.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from bfx_funding_bot.core.sealed_credential import (
    SealContext,
    SealedPayloadError,
    generate_recipient,
    open_sealed,
)

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
RECIPIENT = UUID("11111111-2222-4333-8444-555555555555")
REQUEST = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")
_IDS = (b"\x0011111111-2222-4333-8444-555555555555"
        b"\x0066666666-7777-4888-8999-aaaaaaaaaaaa")


def test_the_seal_info_is_stable() -> None:
    assert SealContext(ACCOUNT, RECIPIENT, REQUEST).info() == (
        b"bfx-credential-rotation/v2\x00aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" + _IDS)


@pytest.mark.parametrize("old_info", [
    # The v1 format, which carried the environment between the account and the recipient.
    b"bfx-credential-rotation/v1\x00aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\x00prod" + _IDS,
    # The v1 label over today's fields: the label alone must tell the formats apart.
    b"bfx-credential-rotation/v1\x00aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee" + _IDS,
])
def test_a_request_sealed_in_the_previous_format_does_not_open(old_info: bytes) -> None:
    """A request still pending across the release is refused, never opened by accident."""
    recipient = generate_recipient()
    suite = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
    sealed = suite.encrypt(
        b'{"api_key":"FAKE-KEY-000","api_secret":"FAKE-SECRET-000"}',
        X25519PublicKey.from_public_bytes(recipient.public_key), info=old_info)
    with pytest.raises(SealedPayloadError) as refused:
        open_sealed(sealed, private_key=recipient.private_half,
                    context=SealContext(ACCOUNT, RECIPIENT, REQUEST))
    assert refused.value.code == "sealed_payload_undecryptable"
