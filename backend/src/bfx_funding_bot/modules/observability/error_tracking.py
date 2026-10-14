"""Error tracking over the Sentry protocol (bot and webapi); off unless ``SENTRY_DSN`` is set.

Events leave the process, so the same rules as the Telegram alerts apply, and more:

- Every string in an event (message, exception values, breadcrumbs, request data,
  tags) is scrubbed of the values :func:`alerts.secret_values` names, then of email
  addresses and IP addresses. ``user`` and request cookies and auth headers are
  dropped whole. A scrubber that fails drops the event: nothing is sent unscrubbed.
- ``include_local_variables=False`` and ``send_default_pii=False``: stack frames
  carry code, not the values (API keys, signed payloads) that were in scope.
- Only the default integrations plus the ones a service passes in. Auto-enabled
  integrations stay off (the httpx one would put request URLs into breadcrumbs and
  trace headers onto venue and Telegram requests), and ``trace_propagation_targets``
  is empty, so no outgoing request changes.
- Delivery is the SDK's background thread with a bounded queue; a capture never
  waits on the network. At exit the SDK flushes for at most ``shutdown_timeout``.

Whether an issue notifies is decided in Sentry (new issue, regression, frequency);
paging stays on symptoms (Grafana rules, the bot's named alerts).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

import sentry_sdk
from sentry_sdk.integrations import Integration

from bfx_funding_bot.modules.observability import alerts

if TYPE_CHECKING:
    from sentry_sdk._types import Event

log = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_S: Final = 2.0
REDACTED: Final = "[redacted]"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}(?![0-9A-Za-z:])"
                   r"|(?<![0-9A-Za-z:])[0-9A-Fa-f:]*::[0-9A-Fa-f:]*[0-9A-Fa-f](?![0-9A-Za-z:])")
_DROPPED_HEADERS: Final = frozenset({"authorization", "cookie", "set-cookie", "x-forwarded-for",
                                     "x-real-ip", "cf-connecting-ip", "true-client-ip"})


def scrub_text(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = _EMAIL.sub(REDACTED, text)
    text = _IPV4.sub(REDACTED, text)
    return _IPV6.sub(REDACTED, text)


def _scrub(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return scrub_text(value, secrets)
    if isinstance(value, Mapping):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item, secrets) for item in value]
    return value


def scrub_event(event: Event, secrets: Sequence[str]) -> Event | None:
    """``before_send``: the event with secrets and personal data removed, or None."""
    try:
        data = cast(dict[str, Any], event)
        data.pop("user", None)
        request = data.get("request")
        if isinstance(request, dict):
            request.pop("cookies", None)
            request.pop("env", None)
            headers = request.get("headers")
            if isinstance(headers, dict):
                request["headers"] = {key: value for key, value in headers.items()
                                      if str(key).lower() not in _DROPPED_HEADERS}
        return cast("Event", _scrub(data, secrets))
    except Exception:  # never send what could not be scrubbed
        return None


def init_error_tracking(service: str, environ: Mapping[str, str], *,
                        integrations: Sequence[Integration] = ()) -> bool:
    """Start the SDK for ``service`` when ``SENTRY_DSN`` is set. Returns whether it did."""
    dsn = environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    secrets = tuple(sorted((s for s in alerts.secret_values(environ) if len(s) >= 6),
                           key=len, reverse=True))
    sentry_sdk.init(
        dsn=dsn,
        release=environ.get("BFX_SERVICE_VERSION") or None,
        environment=environ.get("BFX_DEPLOYMENT_ENV") or "unknown",
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        auto_enabling_integrations=False,
        integrations=list(integrations),
        trace_propagation_targets=[],
        auto_session_tracking=False,
        shutdown_timeout=SHUTDOWN_TIMEOUT_S,
        before_send=lambda event, _hint: scrub_event(event, secrets),
    )
    sentry_sdk.set_tag("service", service)
    log.info("error_tracking_enabled service=%s", service)
    return True


__all__ = ["REDACTED", "SHUTDOWN_TIMEOUT_S", "init_error_tracking", "scrub_event", "scrub_text"]
