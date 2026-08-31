"""HTTP contracts for process liveness and database readiness."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Never

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from bfx_funding_bot.main import app
from bfx_funding_bot.modules.api import deps
from bfx_funding_bot.modules.api.deps import _readiness_timeout_seconds


class SuccessfulSession:
    """Small async session fake that exposes observable probe behavior."""

    def __init__(self) -> None:
        self.closed = False
        self.statements: list[str] = []
        self.committed = False

    async def __aenter__(self) -> SuccessfulSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.closed = True

    async def execute(self, statement: object) -> object:
        self.statements.append(str(statement))
        if "alembic_version" in str(statement):
            return _VersionResult(sorted(deps._expected_alembic_heads()))
        return _VersionResult([1])


class _VersionResult:
    def __init__(self, versions: list[object]) -> None:
        self._versions = versions

    def scalars(self) -> _VersionResult:
        return self

    def all(self) -> list[object]:
        return self._versions


class RaisingSession(SuccessfulSession):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def execute(self, statement: object) -> Never:
        self.statements.append(str(statement))
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
    assert session.statements == ["SELECT 1", "SELECT version_num FROM alembic_version"]
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


@pytest.mark.parametrize("versions", [[], ["a376b830a8f1"], ["f5b8d0e2f3c4", "other"]])
def test_ready_rejects_missing_stale_or_multiple_migration_versions(
    versions: list[str],
) -> None:
    session = SuccessfulSession()
    original_execute = session.execute

    async def execute(statement: object) -> object:
        if "alembic_version" in str(statement):
            return _VersionResult(versions)
        return await original_execute(statement)

    session.execute = execute  # type: ignore[method-assign]
    app.state.session_factory = lambda: session

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}
    assert session.closed is True


def test_readiness_probe_does_not_commit() -> None:
    session = SuccessfulSession()
    app.state.session_factory = lambda: session

    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert session.committed is False


def test_ready_fails_closed_when_migration_graph_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SuccessfulSession()
    app.state.session_factory = lambda: session
    monkeypatch.setattr(deps, "_expected_alembic_heads", lambda: frozenset())

    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "checks": {"database": "failed"}}
    assert session.closed is True
