from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    BitfinexAuthREST,
    KeyPermissions,
    parse_key_permissions,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

# Bitfinex /v2/auth/r/permissions row: [scope, read(0/1), write(0/1)]
_PERMS = [
    ["account", 1, 0],
    ["orders", 0, 0],
    ["funding", 1, 1],
    ["wallets", 1, 0],
    ["withdraw", 0, 0],
]


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("0"),
    )


def test_parse_key_permissions_builds_scope_map():
    perms = parse_key_permissions(_PERMS)
    assert isinstance(perms, KeyPermissions)
    assert perms.can("funding", write=True) is True
    assert perms.can("withdraw", write=True) is False
    assert perms.can("account", write=False) is True
    assert perms.can("nonexistent", write=True) is False


def test_parse_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_key_permissions({"not": "list"})


def test_parse_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_key_permissions([["funding", 1]])


def test_parse_rejects_non_int_flag():
    with pytest.raises(BitfinexShapeError):
        parse_key_permissions([["funding", None, 1]])


@pytest.mark.asyncio
async def test_get_key_permissions_signs_and_parses():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_PERMS)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        perms = await client.get_key_permissions(ctx=_ctx())

    assert perms.can("funding", write=True) is True
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/permissions"
    assert captured["headers"]["bfx-apikey"] == "KEY"
    assert "bfx-signature" in captured["headers"]


@pytest.mark.asyncio
async def test_get_key_permissions_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        with pytest.raises(BitfinexAPIError):
            await client.get_key_permissions(ctx=_ctx())
