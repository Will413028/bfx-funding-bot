"""In-process request authentication, written independently of the client's signer.

Bitfinex: HMAC-SHA384 over `"/api/" + path + nonce + body` keyed with the secret,
hex digest in `bfx-signature`; the nonce must strictly increase per key in arrival
order. A failure answers HTTP 500 with `["error", CODE, MESSAGE]`, exactly as the
venue does (a stale nonce is indistinguishable from an outage without the body).
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

ERR_NONCE_SMALL = (10114, "nonce: small")
ERR_APIKEY_INVALID = (10100, "apikey: invalid")


@dataclass(frozen=True, slots=True)
class RequestAuthenticator:
    """Stateless: the nonce high-water is durable venue state (`VenueState.last_nonce`)."""

    api_key: str
    api_secret: str

    def check(
        self, *, path: str, body: bytes, headers: Mapping[str, str], last_nonce: int,
    ) -> int | tuple[int, str]:
        """The accepted nonce, or the venue error `(code, message)` to answer with.

        Accepting does not record anything: the caller commits the nonce before it
        answers, like every other state change.
        """
        if headers.get("bfx-apikey") != self.api_key:
            return ERR_APIKEY_INVALID
        raw_nonce = headers.get("bfx-nonce", "")
        signature = headers.get("bfx-signature", "")
        expected = hmac.new(
            self.api_secret.encode(), f"/api/{path}{raw_nonce}".encode() + body,
            hashlib.sha384,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return ERR_APIKEY_INVALID
        if not raw_nonce.isdigit() or int(raw_nonce) <= last_nonce:
            return ERR_NONCE_SMALL
        return int(raw_nonce)
