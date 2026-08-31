"""HTTP contracts for process liveness and database readiness."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Never

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from bfx_funding_bot.main import app
from bfx_funding_bot.modules.api.deps import _readiness_timeout_seconds


class SuccessfulSession:
    """Small async session fake that exposes observable probe behavior."""

    def __init__(self) -> None:
        self.closed = False
        self.statement: str | None = None

    async def __aenter__(self) -> SuccessfulSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.closed = True

    async def execute(self, statement: object) -> None:
        self.statement = str(statement)


class RaisingSession(SuccessfulSession):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def execute(self, statement: object) -> Never:
        self.statement = str(statement)
        raise self._error


class RaisingFactory:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def __call__(self) -> Never:
        raise self._error


@pytest.fixture(autouse=True)
def reset_session_factory() -> Iterator[None]:
    previous_factory = getattr(app.state, "session_factory", None)
    app.state.session_factory = None
    try:
        yield
    finally:
        app.state.session_factory = previous_factory


def test_ready_reports_not_ready_without_a_database_factory() -> None:
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}
    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_reports_database_success_and_closes_its_session() -> None:
    session = SuccessfulSession()
    app.state.session_factory = lambda: session
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}
    assert session.statement == "SELECT 1"
    assert session.closed is True
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("error", [SQLAlchemyError(), TimeoutError(), OSError()])
def test_ready_hides_expected_database_errors(error: Exception) -> None:
    session = RaisingSession(error)
    app.state.session_factory = lambda: session
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}
    assert session.closed is True
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("error", [SQLAlchemyError(), TimeoutError(), OSError()])
def test_ready_hides_expected_session_factory_errors(error: Exception) -> None:
    app.state.session_factory = RaisingFactory(error)
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("timeout", ["not-a-number", "nan", "inf", "0", "-1"])
def test_ready_uses_default_timeout_for_invalid_timeout_configuration(
    monkeypatch: pytest.MonkeyPatch, timeout: str
) -> None:
    session = SuccessfulSession()
    app.state.session_factory = lambda: session
    monkeypatch.setenv("BFX_READINESS_TIMEOUT_SECONDS", timeout)
    client = TestClient(app)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


def test_readiness_timeout_is_capped_at_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BFX_READINESS_TIMEOUT_SECONDS", "1000000")

    assert _readiness_timeout_seconds() == 10.0
