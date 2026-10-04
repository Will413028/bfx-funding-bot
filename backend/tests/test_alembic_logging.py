"""Running Alembic in the pytest process must not rewire that process's logging.

``alembic/env.py`` used to call ``fileConfig()`` unconditionally. In-process (the template
builds in ``tests/pg_templates.py``) that set the root logger to WARNING, replaced its
handlers and disabled every logger that already existed, so a later ``caplog`` test saw no
records. Offline ``stamp --sql`` executes ``env.py`` without a database, which is enough to
reach the logging setup.
"""
from __future__ import annotations

import io
import logging

import pytest
from alembic.config import Config

from alembic import command
from tests.pg_templates import ALEMBIC_INI, alembic

_PROBE = "bfx_funding_bot.alembic_logging_probe"


@pytest.fixture
def probe_logger(monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    return logging.getLogger(_PROBE)


def _stamp_offline(*, configure_logger: bool | None) -> None:
    config = Config(str(ALEMBIC_INI), output_buffer=io.StringIO())
    if configure_logger is not None:
        config.attributes["configure_logger"] = configure_logger
    command.stamp(config, "head", sql=True)


def test_in_process_alembic_keeps_loggers_enabled_and_caplog_working(
    probe_logger: logging.Logger, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    level_before, handlers_before = root.level, list(root.handlers)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/none")

    stamp = command.stamp

    def upgrade_offline(config: Config, revision: str) -> None:
        # Same Config the helper built, but rendered as SQL so no database is needed.
        config.output_buffer = io.StringIO()
        stamp(config, revision, sql=True)

    monkeypatch.setattr(command, "upgrade", upgrade_offline)
    alembic("postgresql+psycopg://u:p@127.0.0.1:1/none", "upgrade", "head")

    assert probe_logger.disabled is False
    assert root.level == level_before
    assert list(root.handlers) == handlers_before
    with caplog.at_level(logging.WARNING, logger=_PROBE):
        probe_logger.warning("still captured")
    assert [record.getMessage() for record in caplog.records] == ["still captured"]


def test_cli_logging_setup_is_kept_but_never_disables_existing_loggers(
    probe_logger: logging.Logger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/none")
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    named = {name: logging.getLogger(name).level for name in ("sqlalchemy.engine", "alembic")}
    try:
        _stamp_offline(configure_logger=None)  # what `alembic` on the command line does
        assert logging.getLogger("alembic").level == logging.INFO  # alembic.ini [logger_alembic]
        assert probe_logger.disabled is False
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers
        for name, value in named.items():
            logging.getLogger(name).setLevel(value)
