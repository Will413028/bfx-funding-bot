"""The venue refusing the key is its own error type; nothing else is.

Bitfinex answers a wrong, revoked or swapped key with HTTP 500 and
``["error", 10100, "apikey: ..."]`` (observed in production), and a 401/403 says the same
without the body. Every signed read raises :class:`VenueCredentialRejectedError` for those,
and a plain :class:`BitfinexAPIError` for everything else -- above all ``10114`` (nonce:
small), which the boot must keep refusing.

Mutation checks (one at a time; revert after each):

* drop the 401/403 branch of ``is_credential_rejection``: ``test_a_refused_key_*[401]``.
* classify any parsed venue code as a rejection: ``test_other_errors_*[500_10114]``.
* let a 401/403 status win over the body's code: ``test_other_errors_*[403_10114]``.
"""
from __future__ import annotations

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import (
    BitfinexAPIError,
    BitfinexRateLimited,
    VenueCredentialRejectedError,
    credential_rejection,
    venue_did_not_answer,
)
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

_DIGEST_INVALID = '["error",10100,"apikey: digest invalid"]'


def _ctx() -> AccountContext:
    return AccountContext(account_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                          credentials=Credentials(api_key="KEY", api_secret="SECRET"))


async def _user_id_error(response: httpx.Response) -> BitfinexAPIError:
    transport = httpx.MockTransport(lambda _r: response)
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        with pytest.raises(BitfinexAPIError) as failed:
            await client.get_user_id(ctx=_ctx())
    return failed.value


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text=_DIGEST_INVALID),
        httpx.Response(500, text='["error",10100,"apikey: invalid"]'),
        httpx.Response(401, text="unauthorized"),
        httpx.Response(403, text=""),
    ],
    ids=["500_10100_digest", "500_10100_invalid", "401", "403"],
)
async def test_a_refused_key_raises_the_credential_rejection(response: httpx.Response) -> None:
    error = await _user_id_error(response)
    assert type(error) is VenueCredentialRejectedError
    # Still a BitfinexAPIError with its status and body: existing handling is unchanged.
    assert error.status_code == response.status_code
    assert error.raw == response.text
    assert venue_did_not_answer(error) is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text='["error",10114,"nonce: small"]'),
        httpx.Response(500, text='["error",11010,"ratelimit: error"]'),
        httpx.Response(500, text=""),
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(429, text=""),
        httpx.Response(400, text="bad request"),
        httpx.Response(403, text='["error",10114,"nonce: small"]'),
    ],
    ids=["500_10114", "500_11010", "500_empty", "502_html", "429", "400", "403_10114"],
)
async def test_other_errors_stay_plain_venue_errors(response: httpx.Response) -> None:
    error = await _user_id_error(response)
    assert type(error) is BitfinexAPIError
    assert credential_rejection(error) is None


async def test_a_wallet_read_with_a_refused_key_is_classified_too() -> None:
    """The boot observation's first signed read (``v2/auth/r/wallets``) goes through the
    same choke point as every other read."""
    transport = httpx.MockTransport(lambda _r: httpx.Response(500, text=_DIGEST_INVALID))
    async with httpx.AsyncClient(transport=transport) as http:
        client = BitfinexAuthREST(http=http, auth_gate=AuthRequestGate(lambda: 1))
        with pytest.raises(VenueCredentialRejectedError):
            await client.fetch_wallet_observations(ctx=_ctx())


def _chained(outer: BaseException, inner: BaseException, *, implicit: bool = False) -> BaseException:
    if implicit:
        outer.__context__ = inner
    else:
        outer.__cause__ = inner
    return outer


def test_the_rejection_is_found_anywhere_in_the_chain() -> None:
    typed = VenueCredentialRejectedError(500, "Internal Server Error", _DIGEST_INVALID)
    assert credential_rejection(typed) is typed
    assert credential_rejection(_chained(RuntimeError("cycle"), typed)) is typed
    assert credential_rejection(_chained(RuntimeError("cycle"), typed, implicit=True)) is typed
    # A plain BitfinexAPIError with the same answer (built elsewhere) classifies alike.
    plain = BitfinexAPIError(500, "Internal Server Error", _DIGEST_INVALID)
    found = credential_rejection(_chained(ValueError("wrapped"), plain))
    assert isinstance(found, VenueCredentialRejectedError)
    assert (found.status_code, found.raw) == (500, _DIGEST_INVALID)


@pytest.mark.parametrize(
    "exc",
    [
        BitfinexAPIError(0, "transport error"),
        BitfinexRateLimited(),
        BitfinexAPIError(500, "x", '["error",10114,"nonce: small"]'),
        ValueError("unrelated"),
    ],
)
def test_nothing_else_is_a_rejection(exc: BaseException) -> None:
    assert credential_rejection(exc) is None


def test_only_the_venue_body_makes_a_rejection_venue_coded() -> None:
    assert VenueCredentialRejectedError(500, "x", _DIGEST_INVALID).venue_coded is True
    assert VenueCredentialRejectedError(403, "Forbidden", "<html>waf</html>").venue_coded is False
    assert VenueCredentialRejectedError(401, "Unauthorized", None).venue_coded is False
