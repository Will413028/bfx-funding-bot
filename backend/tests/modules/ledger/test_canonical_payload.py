"""Pin journal payload bytes independently of JSONB's storage order."""

from hashlib import sha256

import pytest

from bfx_funding_bot.modules.ledger._internal.journal import canonical_payload


def test_payload_encoding_is_stable_utf8_json() -> None:
    payload = {"z": [2, 1], "é": "值", "a": {"b": True}}
    encoded = b'{"a":{"b":true},"z":[2,1],"\xc3\xa9":"\xe5\x80\xbc"}'
    assert canonical_payload(payload) == encoded
    assert sha256(canonical_payload(payload)).hexdigest() == sha256(encoded).hexdigest()


def test_payload_rejects_non_json_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_payload({"amount": float("nan")})
