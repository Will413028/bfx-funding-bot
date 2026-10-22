"""The JSON log line (9j): UTC timestamp, escaped values, extras, and who owns the root.

Mutations these catch: dropping ``json.dumps`` for string concatenation (a CR/LF in a value
starts a forged line), a local-time or offset ``ts``, extras not written, a second handler
on a repeated call, a configured root (pytest's) taken over, uvicorn's own handlers kept,
the message moved out of ``msg`` (the Grafana rules match it).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from bfx_funding_bot.core.log_format import UVICORN_LOGGERS, JsonFormatter, configure_logging
from bfx_funding_bot.modules.observability import alerts

# 2026-10-21T01:02:03.456Z
_CREATED = 1792544523.456
_HOSTILE = "ok\nFAKE 2026-10-21 CRITICAL bfx forged line\r\x1b[31mred\x00 sep\x85nel"


def _record(msg: str, *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("bfx.test", logging.WARNING, __file__, 1, msg, args, None)
    record.created = _CREATED
    record.__dict__.update(extra)
    return record


def test_a_line_is_one_json_object_with_a_utc_timestamp() -> None:
    line = JsonFormatter("bot").format(_record("daemon_started phase=%s", "live"))

    assert json.loads(line) == {
        "ts": "2026-10-21T01:02:03.456Z", "level": "WARNING", "service": "bot",
        "logger": "bfx.test", "msg": "daemon_started phase=live",
    }


def test_control_characters_in_any_value_cannot_start_a_line() -> None:
    """F37: a value from a query string or header is data inside one line, never a line."""
    line = JsonFormatter("bot").format(_record("admin %s", _HOSTILE, actor=_HOSTILE))

    assert line.splitlines() == [line]  # splitlines also breaks on \x85 and
    assert re.search(r"[\x00-\x1f\x7f-￿]", line) is None
    payload = json.loads(line)
    assert payload["msg"] == f"admin {_HOSTILE}"
    assert payload["actor"] == _HOSTILE


def test_extras_are_fields_and_cannot_overwrite_the_core_ones() -> None:
    record = _record("x", event="access_denied", reason="missing_auth", ts="forged",
                     color_message="\x1b[1mx", ids=["a", 1])

    payload = json.loads(JsonFormatter("webapi").format(record))

    assert payload["event"] == "access_denied" and payload["reason"] == "missing_auth"
    assert payload["ts"] == "2026-10-21T01:02:03.456Z" and payload["extra_ts"] == "forged"
    assert payload["ids"] == ["a", 1]
    assert "color_message" not in payload


def test_a_traceback_is_one_escaped_field() -> None:
    try:
        raise RuntimeError("boom\nsecond line")
    except RuntimeError:
        record = _record("deployment_submit_error cell=%s", "c1")
        record.exc_info = sys.exc_info()

    line = JsonFormatter("bot").format(record)

    assert "\n" not in line
    payload = json.loads(line)
    assert payload["msg"] == "deployment_submit_error cell=c1"
    assert "RuntimeError: boom\nsecond line" in payload["exc"]


@pytest.mark.parametrize("message", [
    "deployment_skip cell=%s amount=%s dependency=%s reason=%s",
    "deployment_skip_no_amount_fingerprint cell=%s amount=%s dependency=%s reason=%s",
    "deployment_submit_rejected cell=%s amount=%s dependency=%s outcome=%s",
    "deployment_submit_error cell=%s amount=%s dependency=%s reason=%s",
])
def test_the_grafana_rule_substrings_still_match_the_raw_line(message: str) -> None:
    """homelab bfx-rules.yml: ``|= "deployment_skip"`` and
    ``|~ "deployment_submit_rejected|deployment_submit_error"`` run on the raw line."""
    line = JsonFormatter("bot").format(_record(message, "c1", "100", "d", "why"))

    event = message.split(" ", 1)[0]
    assert event in line
    matched = ("deployment_skip" in line
               or re.search("deployment_submit_rejected|deployment_submit_error", line))
    assert matched


def test_the_telegram_token_filter_still_runs_before_the_json_line() -> None:
    """The httpx filter rewrites the record; the formatter encodes the rewritten message."""
    logger = logging.getLogger("httpx")
    stream: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            stream.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(JsonFormatter("bot"))
    logger.addFilter(alerts._HTTPX_TOKEN_FILTER)
    logger.addHandler(handler)
    try:
        logger.warning('HTTP Request: %s %s "%s"', "POST",
                       "https://api.telegram.org/bot123456:SECRETTOKEN/sendMessage", "HTTP/1.1 200 OK")
    finally:
        logger.removeHandler(handler)
        logger.removeFilter(alerts._HTTPX_TOKEN_FILTER)

    [line] = stream
    assert "SECRETTOKEN" not in line
    assert "api.telegram.org/bot[redacted]/sendMessage" in json.loads(line)["msg"]


@contextmanager
def _bare_root() -> Iterator[logging.Logger]:
    """The root as a fresh process has it (no handlers), restored on exit. Entered in the
    test body: pytest adds its capture handlers after the fixtures are set up."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    saved_uvicorn = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
                     for name in UVICORN_LOGGERS}
    root.handlers.clear()
    try:
        yield root
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        for name, (handlers, propagate) in saved_uvicorn.items():
            logging.getLogger(name).handlers[:] = handlers
            logging.getLogger(name).propagate = propagate


def test_configure_logging_owns_a_bare_root_once(capsys: pytest.CaptureFixture[str]) -> None:
    uvicorn_access = logging.getLogger("uvicorn.access")
    with _bare_root() as root:
        uvicorn_access.addHandler(logging.StreamHandler(sys.stdout))  # uvicorn's dictConfig
        uvicorn_access.propagate = False

        assert configure_logging("webapi") is True
        assert configure_logging("webapi", level="WARNING") is True  # the lifespan's level

        assert len(root.handlers) == 1 and root.level == logging.WARNING
        uvicorn_access.warning('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/x", "1.1", 401)
        logging.getLogger("bfx.test").info("below the level")
    out = capsys.readouterr()
    assert out.out == ""  # uvicorn's own handler is gone
    [line] = out.err.splitlines()
    payload = json.loads(line)
    assert (payload["service"], payload["logger"]) == ("webapi", "uvicorn.access")
    assert payload["msg"] == '127.0.0.1:1 - "GET /x HTTP/1.1" 401'


def test_configure_logging_leaves_a_configured_root_alone() -> None:
    """pytest's capture (or a CLI's basicConfig) already owns the root: no takeover."""
    with _bare_root() as root:
        theirs = logging.NullHandler()
        root.addHandler(theirs)
        root.setLevel(logging.ERROR)

        assert configure_logging("bot") is False
        assert root.handlers == [theirs] and root.level == logging.ERROR


_BOT_MAIN = """
import logging
from bfx_funding_bot.apps import bot

async def _run():
    logging.getLogger("bfx.test").warning("deployment_skip cell=%s", "c1\\nforged")

bot._run = _run
bot.main()
"""
_WEBAPI_IMPORT = """
import logging
from bfx_funding_bot.apps.webapi import app
logging.getLogger("uvicorn.access").info("access line")
"""


@pytest.mark.parametrize(("service", "code", "logger", "msg"), [
    ("bot", _BOT_MAIN, "bfx.test", "deployment_skip cell=c1\nforged"),
    ("webapi", _WEBAPI_IMPORT, "uvicorn.access", "access line"),
])
def test_each_process_writes_json_lines_to_stderr(
    service: str, code: str, logger: str, msg: str,
) -> None:
    """The entry points themselves: the bot's ``main`` and the web API's import."""
    environment = {k: v for k, v in os.environ.items() if k != "SENTRY_DSN"}
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2],
                            env=environment, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    payloads = [json.loads(line) for line in result.stderr.splitlines()]
    assert {"service": service, "logger": logger, "msg": msg}.items() <= payloads[-1].items()
    assert payloads[-1]["ts"].endswith("Z")
