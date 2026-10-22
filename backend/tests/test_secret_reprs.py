"""Secret fields stay usable and serializable, but never enter repr/str."""

import asyncio
import json
from collections.abc import Callable
from unittest.mock import Mock

import pytest

from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy
from bfx_funding_bot.modules.marketfeed.config import MarketfeedConfig
from bfx_funding_bot.modules.marketfeed.daemon import Daemon
from bfx_funding_bot.modules.observability.resource import DeploymentEnvironment

DATABASE_URL = "postgresql://u:fake-db-password@h/db"


@pytest.fixture
def config() -> MarketfeedConfig:
    return MarketfeedConfig(
        phase=Phase.SHADOW,
        cells=[],
        database_url=DATABASE_URL,
        deployment_environment=DeploymentEnvironment.CI,
        execution_policy=ExecutionPolicy.BOOK_GUARDED,
    )


@pytest.mark.parametrize("render", [repr, str], ids=["repr", "str"])
def test_daemon_admin_token_hidden(
    config: MarketfeedConfig, render: Callable[[object], str],
) -> None:
    daemon = Daemon(
        config=config,
        registry=Mock(),
        candle_q=asyncio.Queue(),
        diagnostics=Mock(),
        probe=Mock(),
        monitor=Mock(),
        scheduler=Mock(),
        signal_engine=Mock(),
        db_engine=Mock(),
        ws_client=None,
        writer=Mock(),
        bitfinex_http=Mock(),
        bitfinex=Mock(),
        session_factory=Mock(),
        account_bootstrap=Mock(),
        executor=Mock(),
        safety_chain=Mock(),
        account_ctx=Mock(),
        bus=Mock(),
        admin_token="fake-admin-token",
        admin_read_token="fake-read-token",
    )
    assert daemon.admin_token == "fake-admin-token"
    assert "fake-admin-token" not in render(daemon)
    assert "fake-read-token" not in render(daemon)


@pytest.mark.parametrize("render", [repr, str], ids=["repr", "str"])
def test_settings_dsn_hidden_and_serialization_preserved(
    monkeypatch: pytest.MonkeyPatch, render: Callable[[object], str],
) -> None:
    monkeypatch.delenv("BFX_PHASE", raising=False)
    settings = Settings(_env_file=None, database_url=DATABASE_URL)
    assert settings.database_url == DATABASE_URL
    assert settings.model_dump()["database_url"] == DATABASE_URL
    assert json.loads(settings.model_dump_json())["database_url"] == DATABASE_URL
    assert "fake-db-password" not in render(settings)


@pytest.mark.parametrize("render", [repr, str], ids=["repr", "str"])
def test_marketfeed_dsn_hidden_and_serialization_preserved(
    config: MarketfeedConfig, render: Callable[[object], str],
) -> None:
    assert config.database_url == DATABASE_URL
    assert config.model_dump()["database_url"] == DATABASE_URL
    assert json.loads(config.model_dump_json())["database_url"] == DATABASE_URL
    assert "fake-db-password" not in render(config)
