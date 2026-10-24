"""Security events (9j): one WARNING line per refusal on the ``bfx.security`` logger, and
one INFO line per request the operator is let through (ASVS V16.3.1).

The access log already has the status line; these add why and who. Each line carries a
stable ``event`` (below), a ``reason`` code and, when known, the caller as the web API
knows it (the tailnet login Serve forwards, the operator node user ID). Never a token,
a header that carries one, or a request body. Request-supplied strings are clipped and
the JSON formatter (``core/log_format.py``) escapes them.
"""
from __future__ import annotations

import logging
from typing import Final

ACCESS_DENIED: Final = "access_denied"        # 401/403: no or wrong credential/identity
REQUEST_REJECTED: Final = "request_rejected"  # ingress: host, cross-site, content type
RATE_LIMITED: Final = "rate_limited"          # 429
BOOT_REFUSED: Final = "boot_refused"          # a service refused to start
ADMIN_HALT: Final = "admin_halt"              # bot /admin/halt was called (an operator command)
ACCESS_GRANTED: Final = "access_granted"      # INFO: the operator passed require_operator
INPUT_REJECTED: Final = "input_rejected"      # 422: a request failed its model (field paths only)

MAX_FIELD_CHARS: Final = 200

log = logging.getLogger("bfx.security")


def clip(value: str | None) -> str | None:
    """A request-supplied string bounded to MAX_FIELD_CHARS (None stays None)."""
    if value is None or len(value) <= MAX_FIELD_CHARS:
        return value
    return value[:MAX_FIELD_CHARS] + "..."


def security_event(event: str, *, reason: str, level: int = logging.WARNING,
                   **fields: object) -> None:
    """Log ``event`` with ``reason`` and ``fields`` as structured fields; None fields are
    left out. The message is ``"<event> <reason>"``, both fixed codes from this code base."""
    extra: dict[str, object] = {"event": event, "reason": reason}
    extra.update({key: value for key, value in fields.items() if value is not None})
    log.log(level, "%s %s", event, reason, extra=extra)
