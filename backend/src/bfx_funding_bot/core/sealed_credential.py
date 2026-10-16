"""A Bitfinex key sealed to one bot run: HPKE base mode (RFC 9180), no shared secret.

The operator CLI seals a new API key and secret to the public key the running bot
published for this boot (``credential_rotation_recipients``); only that process holds the
private key, in memory, so a sealed credential outlives neither the run nor a restart
(forward secrecy: nothing in the database ever decrypts it again).

Suite ``DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / ChaCha20-Poly1305`` from the locked
``cryptography``. The ``info`` binds the ciphertext to its account, environment, recipient
and request (:meth:`SealContext.info`): moved to any other row it does not open.

The plaintext is ``{"api_key": ..., "api_secret": ...}`` as JSON. Every error here is a
:class:`SealedPayloadError` with a fixed code and never carries the plaintext.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

INFO_LABEL: Final = b"bfx-credential-rotation/v1"
PUBLIC_KEY_BYTES: Final = 32
FIELD_MAX: Final = 128
# The sealed size the outbox accepts (the migration's CHECK): encapsulated key (32) and tag
# (16) around a JSON body of two bounded fields.
SEALED_MIN_BYTES: Final = 64
SEALED_MAX_BYTES: Final = 1024

_SUITE: Final = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)


class SealedPayloadError(ValueError):
    """A credential that cannot be sealed or opened; ``code`` says why, never what."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SealContext:
    """What a sealed credential is bound to."""

    exchange_account_id: UUID
    deployment_environment: str
    recipient_id: UUID
    request_id: UUID

    def info(self) -> bytes:
        if not self.deployment_environment or "\x00" in self.deployment_environment:
            raise SealedPayloadError("invalid_seal_context")
        return b"\x00".join((
            INFO_LABEL, str(self.exchange_account_id).encode(),
            self.deployment_environment.encode(), str(self.recipient_id).encode(),
            str(self.request_id).encode(),
        ))


@dataclass(frozen=True, slots=True)
class Recipient:
    """One run's key pair; the private half never leaves this process."""

    private_half: X25519PrivateKey = field(repr=False)
    public_key: bytes


@dataclass(frozen=True, slots=True)
class UnsealedCredential:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


def generate_recipient() -> Recipient:
    private_key = X25519PrivateKey.generate()
    public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return Recipient(private_half=private_key, public_key=public)


def _check_field(value: object) -> str:
    # Printable ASCII without whitespace: Bitfinex keys are hex-like tokens, and a stray
    # space or newline from the terminal is refused rather than sealed.
    if (not isinstance(value, str) or not 1 <= len(value) <= FIELD_MAX
            or not all("\x21" <= ch <= "\x7e" for ch in value)):
        raise SealedPayloadError("invalid_credential_format")
    return value


def validate_credential(api_key: str, api_secret: str) -> None:
    """Refuse a key or secret this module would not seal (empty, too long, not printable)."""
    _check_field(api_key)
    _check_field(api_secret)


def seal(api_key: str, api_secret: str, *, public_key: bytes, context: SealContext) -> bytes:
    validate_credential(api_key, api_secret)
    if len(public_key) != PUBLIC_KEY_BYTES:
        raise SealedPayloadError("invalid_recipient_key")
    try:
        recipient = X25519PublicKey.from_public_bytes(public_key)
    except ValueError:
        raise SealedPayloadError("invalid_recipient_key") from None
    body = json.dumps({"api_key": api_key, "api_secret": api_secret},
                      separators=(",", ":"), sort_keys=True).encode("ascii")
    return _SUITE.encrypt(body, recipient, info=context.info())


def open_sealed(sealed: bytes, *, private_key: X25519PrivateKey,
                context: SealContext) -> UnsealedCredential:
    """The credential sealed to ``private_key`` for exactly ``context``."""
    if not SEALED_MIN_BYTES <= len(sealed) <= SEALED_MAX_BYTES:
        raise SealedPayloadError("sealed_payload_size")
    try:
        body = _SUITE.decrypt(sealed, private_key, info=context.info())
    except (InvalidTag, ValueError):
        raise SealedPayloadError("sealed_payload_undecryptable") from None
    # ``from None`` throughout: a JSON error keeps the document it failed on.
    try:
        decoded = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SealedPayloadError("sealed_payload_malformed") from None
    if not isinstance(decoded, dict) or set(decoded) != {"api_key", "api_secret"}:
        raise SealedPayloadError("sealed_payload_malformed")
    return UnsealedCredential(api_key=_check_field(decoded["api_key"]),
                              api_secret=_check_field(decoded["api_secret"]))


__all__ = [
    "FIELD_MAX",
    "INFO_LABEL",
    "PUBLIC_KEY_BYTES",
    "SEALED_MAX_BYTES",
    "SEALED_MIN_BYTES",
    "Recipient",
    "SealContext",
    "SealedPayloadError",
    "UnsealedCredential",
    "generate_recipient",
    "open_sealed",
    "seal",
    "validate_credential",
]
