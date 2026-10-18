from __future__ import annotations

import json

from bfx_funding_bot.external.bitfinex.submit_wire import venue_error

# Bitfinex v2 codes this code base decides on (docs.bitfinex.com, "Abbreviations
# Glossary"). Every retry, reconnect or refuse decision on a code goes through
# these names; nothing compares a literal code elsewhere.
#
# Errors, answered as ``["error", CODE, MESSAGE]`` (REST: often with HTTP 500).
# ERR_AUTH_FAIL: the request's key or signature was refused. Observed in
# production as ``["error",10100,"apikey: digest invalid"]`` from the authenticated
# REST endpoints (2026-05-27 smoke with fake credentials; 2026-08-05 key and
# secret swapped). An answer, never "try again".
ERR_AUTH_FAIL = 10100
# ERR_RATE_LIMIT: ``["error", 11010, "ratelimit: error"]``, with HTTP 429 or 200
# (tests/modules/external_signals/test_client.py, tests/external/bitfinex/test_rest.py).
ERR_RATE_LIMIT = 11010
# ERR_READY: "Not ready, try again later" (glossary). Observed in production as
# ``["error",11000,"ready: invalid"]`` with HTTP 500 from v2/auth/r/funding/offers
# (2026-10-14), one request after a 200 from the same account.
ERR_READY = 11000
# Info events on a WebSocket (``{"event": "info", "code": CODE}``).
INFO_SERVER_RESTART = 20051     # the server is restarting: reconnect
INFO_MAINTENANCE = 20060        # maintenance begins
INFO_MAINTENANCE_END = 20061    # maintenance is over: Bitfinex advises resubscribing
# Codes that mean "not now" rather than a refusal; every other venue error code is
# the venue's answer (ERR_AUTH_FAIL, or 10114 "nonce: small").
TRANSIENT_VENUE_ERROR_CODES = frozenset({ERR_RATE_LIMIT, ERR_READY, INFO_MAINTENANCE})
# Info codes asking the client for a fresh connection.
RECONNECT_INFO_CODES = frozenset({INFO_SERVER_RESTART, INFO_MAINTENANCE_END})


class BitfinexError(Exception):
    """Base for all Bitfinex client errors."""


class BitfinexAPIError(BitfinexError):
    """Bitfinex returned a non-2xx response with an error body."""

    def __init__(self, status_code: int, message: str, raw: str | None = None) -> None:
        super().__init__(f"Bitfinex API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.raw = raw


class BitfinexRateLimited(BitfinexAPIError):  # noqa: N818
    """Bitfinex returned 429. Retryable after backoff."""

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__(429, "rate limited")
        self.retry_after_seconds = retry_after_seconds


class BitfinexShapeError(BitfinexError):
    """Response parsed as JSON but didn't match expected shape."""


def venue_did_not_answer(exc: BitfinexAPIError) -> bool:
    """Whether ``exc`` is the venue not answering (retry later), not answering *no*.

    Status 0 is the client's transport error or deadline. A body shaped
    ``["error", CODE, MESSAGE]`` is the venue answering -- Bitfinex sends its
    refusals (``apikey: invalid``, ``nonce: small``) with HTTP 500 -- so only the
    rate-limit, not-ready and maintenance codes are "not now". Without such a body, 429 and
    5xx (a gateway page, an empty body) are the venue declining for now; other
    4xx are answers."""
    if exc.status_code == 0:
        return True
    answer = parsed_venue_error(exc.raw)
    if answer is not None:
        return answer[0] in TRANSIENT_VENUE_ERROR_CODES
    return exc.status_code == 429 or exc.status_code >= 500


def parsed_venue_error(raw: str | None) -> tuple[int, str] | None:
    if not raw:
        return None
    try:
        return venue_error(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
