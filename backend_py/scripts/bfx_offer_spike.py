"""Learning spike: post one Bitfinex funding offer + observe lifecycle.

NOT imported by daemon — runs standalone via:
  cd backend_py && uv run python scripts/bfx_offer_spike.py

Purpose:
- Learn HMAC-SHA384 auth signature shape
- Observe real cid lifecycle (Bitfinex echo, daily uniqueness window)
- Observe real fill latency / partial fill / cancel-already behavior
- Record responses via VCR for 4.4 BitfinexLiveExecutor contract tests

Env required:
  BFX_API_KEY, BFX_API_SECRET — testnet credentials preferred

Outputs JSON to stdout. Pipe to file for VCR setup.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import date
from uuid import uuid4

import httpx

from bfx_funding_bot.external.bitfinex.cid import generate_cid

BFX_BASE = "https://api.bitfinex.com"


def _sign(path: str, body: str, api_secret: str, nonce: str) -> str:
    payload = f"/api/{path}{nonce}{body}"
    return hmac.new(
        api_secret.encode(), payload.encode(), hashlib.sha384,
    ).hexdigest()


async def post_funding_offer(
    *,
    api_key: str,
    api_secret: str,
    symbol: str = "fUSD",
    amount: float = 50.0,
    rate: float = 0.0001,
    period: int = 2,
) -> dict:
    nonce = str(int(time.time() * 1_000_000))
    corr = uuid4()
    cid = generate_cid(corr, date.today())
    body = json.dumps({
        "type": "LIMIT",
        "symbol": symbol,
        "amount": str(amount),
        "rate": str(rate),
        "period": period,
        "flags": 0,
        "cid": cid,
    })
    path = "v2/auth/w/funding/offer/submit"
    sig = _sign(path, body, api_secret, nonce)
    headers = {
        "bfx-nonce": nonce,
        "bfx-apikey": api_key,
        "bfx-signature": sig,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(base_url=BFX_BASE, timeout=30.0) as client:
        resp = await client.post(f"/{path}", headers=headers, content=body)
        return {
            "cid": cid,
            "correlation_id": str(corr),
            "status": resp.status_code,
            "body": resp.text,
        }


async def main() -> int:
    api_key = os.environ.get("BFX_API_KEY")
    api_secret = os.environ.get("BFX_API_SECRET")
    if not api_key or not api_secret:
        print("BFX_API_KEY / BFX_API_SECRET required", file=sys.stderr)
        return 1
    result = await post_funding_offer(api_key=api_key, api_secret=api_secret)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
