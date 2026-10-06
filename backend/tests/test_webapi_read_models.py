"""Boot selection of the web API's read models, the 503 gate, and the evidence reference.

Mutation checks (one at a time; revert after each):

* ``select_read_models`` returns another reader than the ledger's: ``test_select_read_models_*``.
* The lifespan stops requiring the ``ledger`` epoch: ``test_lifespan_*``.
* The lifespan stops storing ``app.state.read_models``: ``test_lifespan_*``.
* ``/ready`` ignores the epoch: ``test_ready_refuses_*``.
* ``_request_model`` keeps ``str(reconcile_event_seq)`` for ledger rows: ``test_request_*``.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException

from bfx_funding_bot.apps import webapi
from bfx_funding_bot.apps.read_models import select_read_models
from bfx_funding_bot.core import authority as authority_module
from bfx_funding_bot.modules.api import deps
from bfx_funding_bot.modules.api.deps import ReadModels, get_read_models
from bfx_funding_bot.modules.api.uncertainties import _request_model
from tests.test_readiness import SuccessfulSession


def test_select_read_models_are_the_ledgers() -> None:
    models = select_read_models()
    assert type(models.operator_reads).__name__ == "LedgerOperatorReads"
    assert type(models.operator_evidence).__name__ == "LedgerOperatorEvidence"
    assert type(models.operator_resolution).__name__ == "LedgerOperatorResolution"


class _Engine:
    async def dispose(self) -> None:
        pass


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_lifespan_stores_the_authority_and_its_read_models(monkeypatch) -> None:
    required: list[object] = []

    async def require_ledger_authority(session: object) -> str:
        required.append(session)
        return "migration b1c2d3e4f5a6 genesis"

    monkeypatch.setattr(webapi, "Settings", lambda: SimpleNamespace(log_level="WARNING"))
    monkeypatch.setattr(webapi, "make_engine", lambda _settings: _Engine())
    monkeypatch.setattr(webapi, "make_session_factory", lambda _engine: _Session)
    monkeypatch.setattr(webapi, "require_ledger_authority", require_ledger_authority)
    app = FastAPI()
    async with webapi.lifespan(app):
        assert len(required) == 1
        assert app.state.authority == "ledger"
        assert type(app.state.read_models.operator_reads).__name__ == "LedgerOperatorReads"


@pytest.mark.asyncio
async def test_lifespan_still_refuses_an_unreadable_authority(monkeypatch) -> None:
    async def require_ledger_authority(_session: object) -> str:
        raise authority_module.AuthorityMismatch("authority_unsupported")

    monkeypatch.setattr(webapi, "Settings", lambda: SimpleNamespace(log_level="WARNING"))
    monkeypatch.setattr(webapi, "make_engine", lambda _settings: _Engine())
    monkeypatch.setattr(webapi, "make_session_factory", lambda _engine: _Session)
    monkeypatch.setattr(webapi, "require_ledger_authority", require_ledger_authority)
    app = FastAPI()
    with pytest.raises(authority_module.AuthorityMismatch):
        async with webapi.lifespan(app):
            pytest.fail("must not boot")
    assert not hasattr(app.state, "read_models")


@pytest.mark.asyncio
async def test_get_read_models_is_a_503_until_booted() -> None:
    absent = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(HTTPException) as refused:
        await get_read_models(absent)  # type: ignore[arg-type]
    assert (refused.value.status_code, refused.value.detail) == (503, "db_not_configured")
    models = select_read_models()
    booted = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(read_models=models)))
    assert await get_read_models(booted) is models  # type: ignore[arg-type]
    assert isinstance(models, ReadModels)


class _EpochSession(SuccessfulSession):
    def __init__(self, latest: str) -> None:
        super().__init__()
        self._latest = latest

    async def execute(self, statement: object) -> object:
        if "capital_authority_epoch" in str(statement):
            self.statements.append(str(statement))
            result = await super().execute("SELECT 1")
            result._versions = [self._latest]  # type: ignore[attr-defined]
            return result
        return await super().execute(statement)


def _request(session: SuccessfulSession, **state: object) -> object:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=lambda: session, **state))
    )


@pytest.mark.asyncio
async def test_ready_refuses_when_the_latest_epoch_is_not_the_booted_authority() -> None:
    match = _EpochSession("ledger")
    assert await deps.database_is_ready(_request(match, authority="ledger")) is True  # type: ignore[arg-type]
    assert any("capital_authority_epoch" in statement for statement in match.statements)
    # A database whose latest epoch is not the ledger (a restored pre-switch backup) is not ready.
    legacy = _EpochSession("legacy")
    assert await deps.database_is_ready(_request(legacy, authority="ledger")) is False  # type: ignore[arg-type]


def _row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "request_id": uuid4(), "uncertainty_id": uuid4(), "action": "mark_not_accepted",
        "state": "requested", "reconcile_event_seq": None, "observation_id": None,
        "created_at_ms": 1, "processed_at_ms": None, "outcome_reason": None,
    }
    return SimpleNamespace(**{**values, **overrides})


def test_request_evidence_ref_is_the_event_seq_for_legacy_and_the_observation_for_ledger() -> None:
    assert _request_model(_row(reconcile_event_seq=42)).evidence_ref == "42"  # type: ignore[arg-type]
    observation = uuid4()
    assert (
        _request_model(_row(observation_id=observation)).evidence_ref  # type: ignore[arg-type]
        == f"ledger:v1:obs:{observation}"
    )
