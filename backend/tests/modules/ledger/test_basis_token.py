"""Ledger tokens pin a query and a read-time command clock, independently of legacy."""

from uuid import UUID

import pytest

from bfx_funding_bot.modules.ledger import encode_basis_token, parse_basis_token

QUERY = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


@pytest.mark.parametrize("revision", [0, 1, 2**63 - 1])
def test_token_round_trip(revision: int) -> None:
    token = encode_basis_token(QUERY, revision)
    assert parse_basis_token(token) == (QUERY, revision)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "1",
        "ledger:v2:bad:0",
        "ledger:v1:bad:0",
        f"ledger:v1:{QUERY}:-1",
        f"ledger:v1:{QUERY}:+1",
        f"ledger:v1:{QUERY}:01",
        f"ledger:v1:{QUERY}: 1",
        f"ledger:v1:{QUERY}:１",
        f"ledger:v1:{QUERY}:1:2",
        f"ledger:v1:{str(QUERY).upper()}:1",
    ],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(ValueError):
        parse_basis_token(token)


def test_negative_revision_is_rejected() -> None:
    with pytest.raises(ValueError):
        encode_basis_token(QUERY, -1)
