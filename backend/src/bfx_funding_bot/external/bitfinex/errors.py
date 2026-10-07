# Venue error codes (``["error", CODE, MESSAGE]``) that mean "not now" rather than a
# refusal. 11010 is the rate limit (modules/external_signals/client.py meets it as
# ``["error", 11010, "ratelimit: error"]``); 20060 is the maintenance notice
# (external/bitfinex/funding_book_ws.py). Every other code is the venue's answer,
# for example 10100 "apikey: invalid" or 10114 "nonce: small".
ERR_RATE_LIMIT = 11010
INFO_MAINTENANCE = 20060
TRANSIENT_VENUE_ERROR_CODES = frozenset({ERR_RATE_LIMIT, INFO_MAINTENANCE})


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
