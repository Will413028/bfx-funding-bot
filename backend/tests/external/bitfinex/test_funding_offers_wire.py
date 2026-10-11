"""Wire-format contract tests for Bitfinex active funding offers.

Two layers:
  - test_fixture_parses: fast, runs in the default CI gate. Replays a captured
    real response fixture through the production parser; guards the
    positional-array layout against parser drift.
  - test_live_contract: gated (@pytest.mark.integration). Real signed
    round-trip against api.bitfinex.com; golden-file capture / drift-compare.
"""
import json
import os
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingOffer,
    BitfinexAuthREST,
    parse_active_funding_offers,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

_FIXTURE = Path(__file__).parent / "fixtures" / "funding_offers_real.json"


def _assert_offer_fields(o: ActiveFundingOffer) -> None:
    """Per-offer invariants implied by the positional-array contract."""
    assert isinstance(o.venue_offer_id, str) and o.venue_offer_id
    assert isinstance(o.amount, Decimal) and o.amount >= 0
    assert isinstance(o.period_days, int) and o.period_days > 0
    assert isinstance(o.status, str) and o.status


def test_fixture_parses():
    raw = json.loads(_FIXTURE.read_text())
    offers = parse_active_funding_offers(raw)  # raises BitfinexShapeError on layout drift
    assert offers, "seed fixture must contain at least one offer"
    for o in offers:
        _assert_offer_fields(o)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_contract():
    """Real signed round-trip against api.bitfinex.com.

    Gated: skipped unless BFX_API_KEY / BFX_API_SECRET are set. Validates that
    the venue accepts our HMAC signature and the live response parses. Golden-file
    mode: with BFX_UPDATE_FIXTURE=1 (or a missing fixture) it writes the captured
    response; otherwise it drift-checks the live layout (structure, not values).

    Pre-live run (the actual (B) deliverable):
        BFX_API_KEY=... BFX_API_SECRET=... BFX_UPDATE_FIXTURE=1 \\
          uv run pytest -m integration -k funding_offers --capture=no
    """
    key = os.environ.get("BFX_API_KEY")
    secret = os.environ.get("BFX_API_SECRET")
    if not key or not secret:
        pytest.skip("requires BFX_API_KEY / BFX_API_SECRET for the live signed round-trip")
    symbol = os.environ.get("BFX_VERIFY_SYMBOL", "fUSD")

    ctx = AccountContext(
        account_id="verify",
        credentials=Credentials(api_key=key, api_secret=secret),
    )
    async with httpx.AsyncClient() as http:
        client = BitfinexAuthREST(http=http)
        raw = await client.fetch_funding_offers_raw(ctx=ctx, symbol=symbol)

    # Real wire format must parse through production code (raises on drift).
    offers = parse_active_funding_offers(raw)
    print(f"\n[live_contract] symbol={symbol} offers={len(offers)}")
    for o in offers:
        print(f"  voi={o.venue_offer_id} amount={o.amount} rate={o.rate} "
              f"period={o.period_days}d status={o.status}")

    if os.environ.get("BFX_UPDATE_FIXTURE") == "1" or not _FIXTURE.exists():
        _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE.write_text(json.dumps(raw, indent=2) + "\n")
        print(f"[live_contract] wrote fixture -> {_FIXTURE}")
    else:
        # Drift-compare: empty offers is a valid live state (account has none);
        # the round-trip + parse already validated the wire. Only check field
        # invariants on whatever offers exist.
        for o in offers:
            _assert_offer_fields(o)
