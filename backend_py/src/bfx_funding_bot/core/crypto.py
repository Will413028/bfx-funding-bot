"""SP2 vault crypto: app-managed envelope encryption for Bitfinex api secrets.

Per-record DEK (AES-256-GCM) wrapped by an env-held KEK. AAD binds the row to
its user_id so a ciphertext can't be replayed under another user. Pure
functions take the KEK as an argument (testable without env / DATABASE_URL);
load_kek() reads BFX_VAULT_KEK from the process env at call time.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_DEK_LEN = 32
_CURRENT_KEY_VERSION = 1


class VaultNotConfiguredError(Exception):
    """BFX_VAULT_KEK missing or malformed."""


@dataclass(frozen=True, slots=True)
class Envelope:
    secret_ciphertext: bytes
    secret_nonce: bytes
    wrapped_dek: bytes
    dek_nonce: bytes
    key_version: int


def load_kek() -> bytes:
    raw = os.environ.get("BFX_VAULT_KEK", "")
    if not raw:
        raise VaultNotConfiguredError("BFX_VAULT_KEK not set")
    try:
        kek = base64.b64decode(raw, validate=True)
    except Exception as e:
        raise VaultNotConfiguredError("BFX_VAULT_KEK is not valid base64") from e
    if len(kek) != 32:
        raise VaultNotConfiguredError(f"BFX_VAULT_KEK must decode to 32 bytes, got {len(kek)}")
    return kek


def encrypt_secret(plaintext: str, *, user_id: str, kek: bytes) -> Envelope:
    aad = user_id.encode("utf-8")
    dek = os.urandom(_DEK_LEN)
    secret_nonce = os.urandom(_NONCE_LEN)
    secret_ct = AESGCM(dek).encrypt(secret_nonce, plaintext.encode("utf-8"), aad)
    dek_nonce = os.urandom(_NONCE_LEN)
    wrapped_dek = AESGCM(kek).encrypt(dek_nonce, dek, aad)
    return Envelope(
        secret_ciphertext=secret_ct,
        secret_nonce=secret_nonce,
        wrapped_dek=wrapped_dek,
        dek_nonce=dek_nonce,
        key_version=_CURRENT_KEY_VERSION,
    )


def decrypt_secret(env: Envelope, *, user_id: str, kek: bytes) -> str:
    aad = user_id.encode("utf-8")
    dek = AESGCM(kek).decrypt(env.dek_nonce, env.wrapped_dek, aad)
    plaintext = AESGCM(dek).decrypt(env.secret_nonce, env.secret_ciphertext, aad)
    return plaintext.decode("utf-8")
