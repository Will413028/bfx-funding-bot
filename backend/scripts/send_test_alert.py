"""Send one test alert down the bot's own alert path (alerts.emit -> Telegram).

Proves that TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in the bot's environment
deliver, the way a real alert does: the sink is built by
AlertSink.from_environment and the alert goes through alerts.emit
(de-duplication, rate limit, secret scrubbing, queue). Touches no database and
no venue. Exit 0 when Telegram accepted the message, 1 when it was only logged
(keys missing or malformed) or delivery failed.

On the VM, inside the running bot container (its environment is bot.env):
  sudo docker exec bfx-bot /app/.venv/bin/python -m scripts.send_test_alert
Locally, from backend/:
  uv run python -m scripts.send_test_alert [--note TEXT]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from bfx_funding_bot.modules.observability import alerts

DELIVERY_TIMEOUT_S = 15.0


async def send(sink: alerts.AlertSink, *, note: str) -> int:
    previous = alerts.install(sink)
    try:
        alerts.emit(alerts.ALERT_TEST, note=note)
        await sink.aclose(timeout_s=DELIVERY_TIMEOUT_S)
    finally:
        alerts.install(previous)
    if sink.counts.get("sent"):
        print("alert_test delivered to Telegram")
        return 0
    print(f"alert_test NOT delivered: {sink.counts}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--note", default="test alert from scripts.send_test_alert; "
                                          "no action needed")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(send(alerts.AlertSink.from_environment(os.environ), note=args.note))


if __name__ == "__main__":
    raise SystemExit(main())
