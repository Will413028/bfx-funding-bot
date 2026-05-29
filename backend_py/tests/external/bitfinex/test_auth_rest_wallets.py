"""FundingWallet parse + get_funding_available REST tests.

Parallels test_auth_rest_credits.py. Bitfinex /v2/auth/r/wallets positional layout:
  [0]=WALLET_TYPE [1]=CURRENCY [2]=BALANCE [3]=UNSETTLED_INTEREST [4]=AVAILABLE_BALANCE ...
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    BitfinexAuthREST,
    FundingWallet,
    parse_wallets,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials


def _wallet_row(
    wallet_type: str = "funding",
    currency: str = "UST",
    balance: float = 550.0,
    available: object = 147.5,
) -> list:
    return [wallet_type, currency, balance, None, available, None, None]


def _ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="KEY", api_secret="SECRET"),
        allocation_cap_usdt=Decimal("570"),
    )


# ── parse_wallets ─────────────────────────────────────────────────────────────


def test_parse_wallets_happy_path():
    wallets = parse_wallets([_wallet_row()])
    assert wallets == [
        FundingWallet(
            wallet_type="funding", currency="UST",
            balance=Decimal("550.0"), available=Decimal("147.5"),
        )
    ]


def test_parse_wallets_null_available_is_zero():
    wallets = parse_wallets([_wallet_row(available=None)])
    assert wallets[0].available == Decimal("0")


def test_parse_wallets_rejects_non_list():
    with pytest.raises(BitfinexShapeError):
        parse_wallets({"not": "a list"})


def test_parse_wallets_rejects_short_row():
    with pytest.raises(BitfinexShapeError):
        parse_wallets([["funding", "UST", 550.0]])  # len < 5, no AVAILABLE_BALANCE


# ── BitfinexAuthREST.get_funding_available ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_funding_available_sums_funding_currency_only():
    rows = [
        _wallet_row(wallet_type="funding", currency="UST", available=147.5),
        _wallet_row(wallet_type="exchange", currency="UST", available=999.0),  # excluded: not funding
        _wallet_row(wallet_type="funding", currency="USD", available=50.0),    # excluded: wrong currency
    ]
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=rows)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 111)
        available = await client.get_funding_available(ctx=_ctx(), currency="UST")

    assert available == Decimal("147.5")
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.bitfinex.com/v2/auth/r/wallets"
    headers = captured["headers"]
    assert headers["bfx-apikey"] == "KEY"
    assert "bfx-signature" in headers


@pytest.mark.asyncio
async def test_get_funding_available_zero_when_no_funding_wallet():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=[_wallet_row(wallet_type="exchange")])
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        available = await client.get_funding_available(ctx=_ctx(), currency="UST")
    assert available == Decimal("0")


@pytest.mark.asyncio
async def test_get_funding_available_raises_on_http_error():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, nonce_provider=lambda: 1)
        with pytest.raises(BitfinexAPIError):
            await client.get_funding_available(ctx=_ctx(), currency="UST")
