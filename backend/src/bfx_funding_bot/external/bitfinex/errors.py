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
