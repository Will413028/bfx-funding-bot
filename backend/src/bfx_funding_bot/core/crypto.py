"""App-managed envelope encryption for Bitfinex api secrets.

Per-record DEK (AES-256-GCM) wrapped by a KEK. AAD binds the row to its
exchange account's canonical UUID so a ciphertext can't be replayed under another
account (``modules.credentials.vault``). Pure
functions take the KEK as an argument (testable without env / DATABASE_URL);
load_kek() reads BFX_VAULT_KEK at call time through core.secret_files: the bot's
mounted secret file (BFX_SECRETS_DIR), else the process env.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bfx_funding_bot.core.secret_files import SecretFileError, read_secret

_NONCE_LEN = 12
_DEK_LEN = 32
_CURRENT_KEY_VERSION = 1


class VaultNotConfiguredError(Exception):
    """BFX_VAULT_KEK missing (env unset, or its secret file missing or empty) or malformed."""


@dataclass(frozen=True, slots=True)
class Envelope:
    secret_ciphertext: bytes
    secret_nonce: bytes
    wrapped_dek: bytes
    dek_nonce: bytes
    key_version: int


def load_kek() -> bytes:
    try:
        raw = read_secret("BFX_VAULT_KEK")
    except SecretFileError as exc:
        raise VaultNotConfiguredError(str(exc)) from None
    if raw is None:
        raise VaultNotConfiguredError("BFX_VAULT_KEK not set")
    try:
        kek = base64.b64decode(raw, validate=True)
    except Exception as e:
        raise VaultNotConfiguredError("BFX_VAULT_KEK is not valid base64") from e
    if len(kek) != 32:
        raise VaultNotConfiguredError(f"BFX_VAULT_KEK must decode to 32 bytes, got {len(kek)}")
    return kek


def encrypt_secret_with_aad(plaintext: str, *, aad: str, kek: bytes) -> Envelope:
    """Encrypt a secret bound to an explicit, non-empty AAD string.

    Callers that use account identity must pass the canonical lowercase UUID
    string.
    """
    if not aad:
        raise ValueError("AAD must not be empty")
    aad_bytes = aad.encode("utf-8")
    dek = os.urandom(_DEK_LEN)
    secret_nonce = os.urandom(_NONCE_LEN)
    secret_ct = AESGCM(dek).encrypt(secret_nonce, plaintext.encode("utf-8"), aad_bytes)
    dek_nonce = os.urandom(_NONCE_LEN)
    wrapped_dek = AESGCM(kek).encrypt(dek_nonce, dek, aad_bytes)
    return Envelope(
        secret_ciphertext=secret_ct,
        secret_nonce=secret_nonce,
        wrapped_dek=wrapped_dek,
        dek_nonce=dek_nonce,
        key_version=_CURRENT_KEY_VERSION,
    )


def decrypt_secret_with_aad(env: Envelope, *, aad: str, kek: bytes) -> str:
    """Decrypt a secret using the exact explicit AAD used at encryption time."""
    if not aad:
        raise ValueError("AAD must not be empty")
    aad_bytes = aad.encode("utf-8")
    dek = AESGCM(kek).decrypt(env.dek_nonce, env.wrapped_dek, aad_bytes)
    plaintext = AESGCM(dek).decrypt(env.secret_nonce, env.secret_ciphertext, aad_bytes)
    return plaintext.decode("utf-8")
