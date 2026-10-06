"""Boot selection of the web API's read models, the 503 gate, and the evidence reference.

Mutation checks (one at a time; revert after each):

* ``build_read_models`` returns another reader than the ledger's: ``test_build_read_models_*``.
* The lifespan stops requiring the ``ledger`` epoch: ``test_lifespan_*``.
* The lifespan stops storing ``app.state.read_models``: ``test_lifespan_*``.
* ``_request_model`` stops citing the row's observation: ``test_request_*``.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException

from bfx_funding_bot.apps import webapi
from bfx_funding_bot.apps.read_models import build_read_models
from bfx_funding_bot.core import authority as authority_module
from bfx_funding_bot.modules.api.deps import ReadModels, get_read_models
from bfx_funding_bot.modules.api.uncertainties import _request_model


def test_build_read_models_are_the_ledgers() -> None:
    models = build_read_models()
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
    models = build_read_models()
    booted = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(read_models=models)))
    assert await get_read_models(booted) is models  # type: ignore[arg-type]
    assert isinstance(models, ReadModels)


def _row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "request_id": uuid4(), "uncertainty_id": uuid4(), "action": "mark_not_accepted",
        "state": "requested", "observation_id": uuid4(),
        "created_at_ms": 1, "processed_at_ms": None, "outcome_reason": None,
    }
    return SimpleNamespace(**{**values, **overrides})


def test_request_evidence_ref_is_the_observation() -> None:
    observation = uuid4()
    assert (
        _request_model(_row(observation_id=observation)).evidence_ref  # type: ignore[arg-type]
        == f"ledger:v1:obs:{observation}"
    )
