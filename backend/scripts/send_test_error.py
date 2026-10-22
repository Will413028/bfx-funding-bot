"""Send one test exception down a service's error-tracking path (Sentry protocol).

Proves that SENTRY_DSN in the service's environment reaches Sentry the way a real
error does: the SDK is started by init_error_tracking with the service's own
settings and the exception goes through the same scrubbing. The message carries a
synthetic email and IP address; the event in Sentry must show them as
``[redacted]``. Touches no database and no venue. Exit 0 when the event was handed
to the SDK and flushed, 1 when SENTRY_DSN is not set. Whether it arrived is read in
Sentry (the printed event id).

On the VM, inside the running containers (bot.env / webapi.env plus their secret files,
which the scrub list covers):
  sudo docker exec bfx-bot /app/.venv/bin/python -m scripts.send_test_error --service bot
  sudo docker exec bfx-webapi /app/.venv/bin/python -m scripts.send_test_error --service webapi
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import sentry_sdk

from bfx_funding_bot.modules.observability.error_tracking import init_error_tracking

FLUSH_TIMEOUT_S = 10.0
SYNTHETIC_EMAIL = "error-test@example.com"
SYNTHETIC_IP = "203.0.113.7"


class ErrorTrackingTestError(RuntimeError):
    """Raised on purpose by scripts.send_test_error; no action needed."""


def send(service: str) -> int:
    if not init_error_tracking(service, os.environ):
        print("error tracking NOT configured: SENTRY_DSN is not set", file=sys.stderr)
        return 1
    try:
        raise ErrorTrackingTestError(f"test error from {service}; {SYNTHETIC_EMAIL} at "
                                f"{SYNTHETIC_IP} must arrive redacted")
    except ErrorTrackingTestError:
        event_id = sentry_sdk.capture_exception()
    sentry_sdk.flush(timeout=FLUSH_TIMEOUT_S)
    print(f"test error sent: service={service} event_id={event_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", choices=("bot", "webapi"), required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return send(args.service)


if __name__ == "__main__":
    raise SystemExit(main())
