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


class VenueCredentialRejectedError(BitfinexAPIError):
    """Bitfinex refused the request's key or signature: ``["error", ERR_AUTH_FAIL, ...]``
    (sent with HTTP 500) or HTTP 401/403 (:func:`is_credential_rejection`).

    An answer, not an outage: retrying the same key changes nothing until the key is fixed
    at Bitfinex or replaced. A :class:`BitfinexAPIError`, so every existing ``except``,
    ``status_code`` check and :func:`venue_did_not_answer` treats it as before; only the
    boot (credential recovery mode) and the periodic reconcile (exit 78) single it out."""

    @property
    def venue_coded(self) -> bool:
        """Whether the venue's own body named the key (``ERR_AUTH_FAIL``), not a bare 401/403
        that a gateway in front of the venue (a WAF page) can also send."""
        answer = parsed_venue_error(self.raw)
        return answer is not None and answer[0] == ERR_AUTH_FAIL


class BitfinexShapeError(BitfinexError):
    """Response parsed as JSON but didn't match expected shape."""


def venue_did_not_answer(exc: BitfinexAPIError) -> bool:
    """Whether ``exc`` is the venue not answering (retry later), not answering *no*.

    Status 0 is the client's transport error or deadline. A body shaped
    ``["error", CODE, MESSAGE]`` is the venue answering -- Bitfinex sends its
    refusals (``apikey: invalid``, ``nonce: small``) with HTTP 500 -- so only the
    rate-limit and maintenance codes are "not now". Without such a body, 429 and
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


# HTTP statuses that name the credential itself as refused.
CREDENTIAL_REJECTED_STATUSES = frozenset({401, 403})


def is_credential_rejection(status_code: int, raw: str | None) -> bool:
    """Whether an HTTP error response is the venue refusing the key or signature.

    Bitfinex answers a wrong, revoked or swapped key with HTTP 500 and
    ``["error", 10100, "apikey: ..."]``; a 401 or 403 says the same without the venue body.
    Any other code (``10114`` nonce: small, rate limit, maintenance), whatever the status,
    or a body-less 5xx is not a credential rejection."""
    answer = parsed_venue_error(raw)
    if answer is not None:
        return answer[0] == ERR_AUTH_FAIL
    return status_code in CREDENTIAL_REJECTED_STATUSES


def credential_rejection(exc: BaseException) -> VenueCredentialRejectedError | None:
    """The credential rejection anywhere in ``exc``'s chain (``__cause__`` and
    ``__context__``), or None.

    A :class:`VenueCredentialRejectedError` is returned as is; a plain :class:`BitfinexAPIError`
    whose status and body classify as a rejection (:func:`is_credential_rejection`) is
    returned as one, so a caller that wraps or rebuilds the error is classified alike."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, VenueCredentialRejectedError):
            return current
        if (isinstance(current, BitfinexAPIError) and current.status_code != 0
                and is_credential_rejection(current.status_code, current.raw)):
            return VenueCredentialRejectedError(current.status_code, current.message, current.raw)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None
