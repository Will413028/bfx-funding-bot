"""One JSON object per log line for the bot and the web API (9j), stdlib only.

Each line carries ``ts`` (UTC, ISO-8601, ``Z``), ``level``, ``service``, ``logger`` and
``msg`` (the formatted message, the text the Grafana rules and runbook greps match), plus
``exc`` for a traceback and every ``extra`` field the call passed (``event``, ``reason`` ...).
``json.dumps`` escapes CR, LF and every other control character, so no value -- a query
parameter, a header -- can start a line of its own (F37); ``ensure_ascii`` escapes the
Unicode line separators too.

Redaction stays where it was: alert text is scrubbed before it is logged and the httpx
filter rewrites the record (``modules/observability/alerts.py``); this formatter only
encodes what the record says.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_RECORD_ATTRIBUTES: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
# uvicorn passes an ANSI-coloured copy of its message as an extra.
_IGNORED_EXTRAS: Final = frozenset({"color_message"})
_CORE_FIELDS: Final = ("ts", "level", "service", "logger", "msg", "exc", "stack")
# uvicorn's loggers; their records go to the root handler like every other record.
UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
                          .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RECORD_ATTRIBUTES or key in _IGNORED_EXTRAS or key.startswith("_"):
                continue
            payload[f"extra_{key}" if key in _CORE_FIELDS else key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=True)


class _JsonStderrHandler(logging.Handler):
    """Writes to whatever ``sys.stderr`` is at emit time (as ``logging.lastResort`` does)."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter(service))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except RecursionError:
            raise
        except Exception:
            self.handleError(record)


def configure_logging(service: str, *, level: int | str = logging.INFO) -> bool:
    """Route every record through one JSON handler on the root logger, at ``level``.

    Like ``logging.basicConfig``, it leaves alone a root logger someone else already
    configured (pytest's capture, a CLI) and returns False; called again it only changes
    the level. uvicorn's loggers lose their own handlers and propagate to the root, so
    the web API's access and error lines are JSON too (uvicorn configures them before
    it imports the app)."""
    root = logging.getLogger()
    ours = [h for h in root.handlers if isinstance(h, _JsonStderrHandler)]
    if root.handlers and not ours:
        return False
    if not ours:
        root.addHandler(_JsonStderrHandler(service))
    root.setLevel(level)
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    return True
