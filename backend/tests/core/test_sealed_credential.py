"""``core.sealed_credential``: a key sealed to one run, one account, one request.

Importing ``cryptography.hazmat.primitives.hpke`` here is the version guard: a lock that
resolves a ``cryptography`` without HPKE fails this module at collection.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hpke

from bfx_funding_bot.core.sealed_credential import (
    FIELD_MAX,
    SEALED_MAX_BYTES,
    SEALED_MIN_BYTES,
    SealContext,
    SealedPayloadError,
    generate_recipient,
    open_sealed,
    seal,
)

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
KEY = "FAKE-KEY-0000000000000000000000000000000000"
SECRET = "FAKE-SECRET-not-a-real-bitfinex-secret-0000"


def _context(**change: Any) -> SealContext:
    values: dict[str, Any] = {"exchange_account_id": ACCOUNT, "deployment_environment": "ci",
                              "recipient_id": UUID(int=1), "request_id": UUID(int=2)}
    return SealContext(**{**values, **change})


def test_the_locked_cryptography_has_hpke() -> None:
    assert hpke.Suite is not None


def test_a_sealed_credential_opens_for_its_recipient_and_context() -> None:
    recipient = generate_recipient()
    sealed = seal(KEY, SECRET, public_key=recipient.public_key, context=_context())
    assert SEALED_MIN_BYTES <= len(sealed) <= SEALED_MAX_BYTES
    assert KEY.encode() not in sealed and SECRET.encode() not in sealed
    opened = open_sealed(sealed, private_key=recipient.private_key, context=_context())
    assert (opened.api_key, opened.api_secret) == (KEY, SECRET)
    assert SECRET not in repr(opened) and KEY not in repr(opened)
    assert "private_key" not in repr(recipient)


@pytest.mark.parametrize("change", [
    {"exchange_account_id": uuid4()},
    {"deployment_environment": "prod"},
    {"recipient_id": UUID(int=9)},
    {"request_id": UUID(int=9)},
])
def test_any_other_context_does_not_open(change: dict[str, Any]) -> None:
    """The ciphertext moved to another account, environment, recipient or request."""
    recipient = generate_recipient()
    sealed = seal(KEY, SECRET, public_key=recipient.public_key, context=_context())
    with pytest.raises(SealedPayloadError) as refused:
        open_sealed(sealed, private_key=recipient.private_key, context=_context(**change))
    assert refused.value.code == "sealed_payload_undecryptable"


def test_another_recipient_does_not_open() -> None:
    sealed = seal(KEY, SECRET, public_key=generate_recipient().public_key, context=_context())
    with pytest.raises(SealedPayloadError):
        open_sealed(sealed, private_key=generate_recipient().private_key, context=_context())


@pytest.mark.parametrize("position", [0, 31, 40, -1])
def test_a_tampered_byte_does_not_open(position: int) -> None:
    recipient = generate_recipient()
    sealed = bytearray(seal(KEY, SECRET, public_key=recipient.public_key, context=_context()))
    sealed[position] ^= 0x01
    with pytest.raises(SealedPayloadError):
        open_sealed(bytes(sealed), private_key=recipient.private_key, context=_context())


@pytest.mark.parametrize(("api_key", "api_secret"), [
    ("", SECRET), (KEY, ""), ("x" * (FIELD_MAX + 1), SECRET), (KEY, "y" * (FIELD_MAX + 1)),
    ("key with space", SECRET), (KEY, "secret\n"), ("kéy", SECRET),
])
def test_an_empty_oversized_or_unprintable_field_is_refused(api_key: str, api_secret: str) -> None:
    recipient = generate_recipient()
    with pytest.raises(SealedPayloadError) as refused:
        seal(api_key, api_secret, public_key=recipient.public_key, context=_context())
    assert refused.value.code == "invalid_credential_format"
    assert api_secret not in str(refused.value) or not api_secret


def test_the_longest_fields_fit_the_outbox_bound() -> None:
    recipient = generate_recipient()
    sealed = seal("k" * FIELD_MAX, "s" * FIELD_MAX, public_key=recipient.public_key,
                  context=_context())
    assert len(sealed) <= SEALED_MAX_BYTES


def test_errors_never_carry_the_plaintext() -> None:
    recipient = generate_recipient()
    sealed = seal(KEY, SECRET, public_key=recipient.public_key, context=_context())
    with pytest.raises(SealedPayloadError) as refused:
        open_sealed(sealed, private_key=recipient.private_key, context=_context(request_id=uuid4()))
    rendered = f"{refused.value!r} {refused.value} {refused.value.__cause__!r} {refused.value.__context__!r}"
    assert SECRET not in rendered and KEY not in rendered
    assert refused.value.__cause__ is None


@pytest.mark.parametrize("size", [0, SEALED_MIN_BYTES - 1, SEALED_MAX_BYTES + 1])
def test_a_payload_outside_the_size_bound_is_refused(size: int) -> None:
    with pytest.raises(SealedPayloadError) as refused:
        open_sealed(b"\x00" * size, private_key=generate_recipient().private_key,
                    context=_context())
    assert refused.value.code == "sealed_payload_size"


def test_a_recipient_key_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(SealedPayloadError) as refused:
        seal(KEY, SECRET, public_key=b"\x01" * 31, context=_context())
    assert refused.value.code == "invalid_recipient_key"
