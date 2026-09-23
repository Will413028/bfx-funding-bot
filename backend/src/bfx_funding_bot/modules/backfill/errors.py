"""Exceptions raised inside walking-back service loops."""


class BackfillCursorStuck(Exception):  # noqa: N818
    """Walking-back loop's cursor failed to advance.

    Bitfinex returned a page whose oldest mts is >= our current end_ms,
    which would cause an infinite loop. Most likely cause: API behaviour
    changed (no longer strictly < end_ms). Service raises this rather
    than silently looping.
    """

    def __init__(self, symbol: str, end_ms: int, oldest_mts: int) -> None:
        super().__init__(
            f"cursor stuck: symbol={symbol} end_ms={end_ms} "
            f"oldest_mts={oldest_mts} (expected oldest < end)"
        )
        self.symbol = symbol
        self.end_ms = end_ms
        self.oldest_mts = oldest_mts
