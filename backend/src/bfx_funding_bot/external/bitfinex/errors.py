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
# Info events on a WebSocket (``{"event": "info", "code": CODE}``).
INFO_SERVER_RESTART = 20051     # the server is restarting: reconnect
INFO_MAINTENANCE = 20060        # maintenance begins
INFO_MAINTENANCE_END = 20061    # maintenance is over: Bitfinex advises resubscribing
# Codes that mean "not now" rather than a refusal; every other venue error code is
# the venue's answer (ERR_AUTH_FAIL, or 10114 "nonce: small").
TRANSIENT_VENUE_ERROR_CODES = frozenset({ERR_RATE_LIMIT, INFO_MAINTENANCE})
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
