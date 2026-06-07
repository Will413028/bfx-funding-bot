import base64

import pytest
from cryptography.exceptions import InvalidTag

from bfx_funding_bot.core.crypto import (
    Envelope,
    VaultNotConfiguredError,
    decrypt_secret,
    encrypt_secret,
    load_kek,
)

_KEK = base64.b64encode(bytes(range(32))).decode()  # deterministic 32-byte KEK


def test_round_trip():
    kek = base64.b64decode(_KEK)
    env = encrypt_secret("super-secret", user_id="user_abc", kek=kek)
    assert isinstance(env, Envelope)
    assert env.secret_ciphertext != b"super-secret"
    assert env.key_version == 1
    assert decrypt_secret(env, user_id="user_abc", kek=kek) == "super-secret"


def test_nonce_is_unique_per_call():
    kek = base64.b64decode(_KEK)
    a = encrypt_secret("x", user_id="u", kek=kek)
    b = encrypt_secret("x", user_id="u", kek=kek)
    assert a.secret_nonce != b.secret_nonce
    assert a.secret_ciphertext != b.secret_ciphertext


def test_tampered_ciphertext_fails():
    kek = base64.b64decode(_KEK)
    env = encrypt_secret("x", user_id="u", kek=kek)
    bad = Envelope(
        secret_ciphertext=env.secret_ciphertext[:-1] + bytes([env.secret_ciphertext[-1] ^ 0x01]),
        secret_nonce=env.secret_nonce,
        wrapped_dek=env.wrapped_dek,
        dek_nonce=env.dek_nonce,
        key_version=env.key_version,
    )
    with pytest.raises(InvalidTag):
        decrypt_secret(bad, user_id="u", kek=kek)


def test_wrong_user_aad_fails():
    kek = base64.b64decode(_KEK)
    env = encrypt_secret("x", user_id="alice", kek=kek)
    with pytest.raises(InvalidTag):
        decrypt_secret(env, user_id="bob", kek=kek)


def test_wrong_kek_fails():
    kek = base64.b64decode(_KEK)
    other = bytes([b ^ 0xFF for b in kek])
    env = encrypt_secret("x", user_id="u", kek=kek)
    with pytest.raises(InvalidTag):
        decrypt_secret(env, user_id="u", kek=other)


def test_load_kek_missing_raises(monkeypatch):
    monkeypatch.delenv("BFX_VAULT_KEK", raising=False)
    with pytest.raises(VaultNotConfiguredError):
        load_kek()


def test_load_kek_wrong_length_raises(monkeypatch):
    monkeypatch.setenv("BFX_VAULT_KEK", base64.b64encode(b"too-short").decode())
    with pytest.raises(VaultNotConfiguredError):
        load_kek()


def test_load_kek_ok(monkeypatch):
    monkeypatch.setenv("BFX_VAULT_KEK", _KEK)
    assert len(load_kek()) == 32


@pytest.mark.parametrize("plaintext", ["", "密碼🔑", "super-secret", "with\nnewline"])
def test_round_trip_various_plaintexts(plaintext):
    kek = base64.b64decode(_KEK)
    env = encrypt_secret(plaintext, user_id="u", kek=kek)
    assert decrypt_secret(env, user_id="u", kek=kek) == plaintext
