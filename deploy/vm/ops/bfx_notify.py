#!/usr/bin/env python3
"""Send one operator alert to Telegram; never make the caller fail.

Host-side, standard library only. Credentials come from a root-only env file
(default /opt/bfx/runtime/notify.env) with TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID. A missing file, a malformed value, a network error or a
Telegram refusal is logged to stderr (the journal under systemd) and reported as
"not delivered"; the CLI still exits 0 so an alert can never turn a successful
deploy, backup or restore into a failure. The token is part of the request URL,
so no exception text or URL is ever logged.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path("/opt/bfx/runtime/notify.env")
LEVELS = ("info", "warning", "critical")
_TOKEN = re.compile(r"[0-9]{3,20}:[A-Za-z0-9_-]{20,100}")
_CHAT_ID = re.compile(r"-?[0-9]{1,20}|@[A-Za-z0-9_]{4,64}")
_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
_MAX_TEXT = 3900
_TIMEOUT_SECONDS = 10.0

# (url, body, timeout) -> HTTP status. Raises OSError/URLError on transport failure.
Transport = Callable[[str, bytes, float], int]


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    token: str
    chat_id: str


def _log(message: str) -> None:
    print(f"bfx-notify: {message}", file=sys.stderr, flush=True)


def load_config(path: Path) -> NotifyConfig | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log(f"not configured ({path} missing)")
        return None
    except (OSError, UnicodeError):
        _log(f"config unreadable ({path})")
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or _KEY.fullmatch(key) is None:
            _log("config malformed")
            return None
        values[key] = value.strip()
    token = values.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = values.get("TELEGRAM_CHAT_ID", "")
    if _TOKEN.fullmatch(token) is None or _CHAT_ID.fullmatch(chat_id) is None:
        _log("config incomplete or malformed (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return None
    return NotifyConfig(token=token, chat_id=chat_id)


def _urllib_transport(url: str, body: bytes, timeout: float) -> int:
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def format_message(text: str, *, level: str, host: str | None = None) -> str:
    host = host or socket.gethostname()
    message = f"[bfx][{level.upper()}][{host}] {text.strip()}"
    return message if len(message) <= _MAX_TEXT else message[: _MAX_TEXT - 3] + "..."


def send(
    text: str,
    *,
    level: str = "warning",
    config_path: Path = DEFAULT_CONFIG,
    transport: Transport | None = None,
    host: str | None = None,
) -> bool:
    """Return True only when Telegram accepted the message. Never raises."""
    try:
        if level not in LEVELS:
            level = "warning"
        message = format_message(text, level=level, host=host)
        config = load_config(config_path)
        if config is None:
            _log(f"not delivered: {message}")
            return False
        body = json.dumps(
            {"chat_id": config.chat_id, "text": message, "disable_web_page_preview": True}
        ).encode("utf-8")
        url = f"https://api.telegram.org/bot{config.token}/sendMessage"
        status = (transport or _urllib_transport)(url, body, _TIMEOUT_SECONDS)
        if status != 200:
            _log(f"not delivered (HTTP {status}): {message}")
            return False
        return True
    except Exception as exc:  # an alert path must never raise into its caller
        _log(f"not delivered ({type(exc).__name__}): {text.strip()[:200]}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", choices=LEVELS, default="warning")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("message", nargs="*", help="message text (default: stdin)")
    try:
        args = parser.parse_args(argv)
        text = " ".join(args.message) if args.message else sys.stdin.read()
    except SystemExit:
        return 0
    except Exception as exc:
        _log(f"not delivered ({type(exc).__name__})")
        return 0
    if not text.strip():
        _log("empty message ignored")
        return 0
    send(text, level=args.level, config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
