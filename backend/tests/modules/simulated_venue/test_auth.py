"""In-process HMAC-SHA384 and nonce verification, checked independently of the client signer."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from tests.modules.simulated_venue.helpers import API_KEY, API_SECRET, World, make_world

PATH = "v2/auth/r/wallets"


def signed(nonce: int, *, path: str = PATH, body: bytes = b"{}", secret: str = API_SECRET,
           key: str = API_KEY) -> dict[str, str]:
    digest = hmac.new(secret.encode(), f"/api/{path}{nonce}".encode() + body,
                      hashlib.sha384).hexdigest()
    return {"bfx-apikey": key, "bfx-nonce": str(nonce), "bfx-signature": digest,
            "content-type": "application/json"}


async def post(w: World, headers: dict[str, str], *, path: str = PATH,
               body: bytes = b"{}") -> httpx.Response:
    return await w.venue.client().post(
        f"https://api.bitfinex.com/{path}", content=body, headers=headers)


async def test_a_correct_independent_signature_is_accepted() -> None:
    w = await make_world(funds={"UST": "1000"})
    response = await post(w, signed(10))
    assert response.status_code == 200 and response.json()[0][1] == "UST"


async def test_bad_signature_wrong_secret_wrong_key_and_tampered_body_answer_500_error() -> None:
    w = await make_world(funds={"UST": "1000"})
    bad = [
        signed(10, secret="other"),
        signed(11, key="OTHER_KEY"),
        {**signed(12), "bfx-signature": "00" * 48},
        signed(13, path="v2/auth/r/funding/offers"),   # signed for another path
    ]
    for headers in bad:
        response = await post(w, headers)
        assert response.status_code == 500, headers
        assert response.json()[0] == "error" and response.json()[2].startswith("apikey")
    tampered = await post(w, signed(14, body=b"{}"), body=b'{"x":1}')
    assert tampered.status_code == 500
    missing = await post(w, {"content-type": "application/json"})
    assert missing.status_code == 500


async def test_nonce_must_strictly_increase_in_arrival_order() -> None:
    w = await make_world(funds={"UST": "1000"})
    assert (await post(w, signed(100))).status_code == 200
    for stale in (100, 99, 1):
        response = await post(w, signed(stale))
        assert response.status_code == 500
        assert response.json() == ["error", 10114, "nonce: small"]
    assert (await post(w, signed(101))).status_code == 200


async def test_a_rejected_request_does_not_burn_the_nonce_or_touch_state() -> None:
    w = await make_world(funds={"UST": "1000"})
    assert (await post(w, signed(500, secret="other"))).status_code == 500
    assert (await post(w, signed(500))).status_code == 200  # same nonce, now valid
    body = json.dumps({"type": "LIMIT", "symbol": "fUST", "amount": "150", "rate": "0.0002",
                       "period": 2, "flags": 0}).encode()
    path = "v2/auth/w/funding/offer/submit"
    refused = await post(w, signed(600, path=path, body=body, secret="other"), path=path, body=body)
    assert refused.status_code == 500 and not w.venue.state.offers
