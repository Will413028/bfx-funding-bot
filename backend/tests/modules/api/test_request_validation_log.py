"""A request that fails its model is logged as a security event (ASVS V16.3.3): the field
paths that failed and the caller, never the values; the 422 body stays FastAPI's own."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from bfx_funding_bot.core.auth import LOGIN_HEADER
from bfx_funding_bot.modules.api.schemas import install_request_validation_log


class Body(BaseModel):
    amount: int
    note: str


def _client() -> TestClient:
    app = FastAPI()
    install_request_validation_log(app)

    @app.post("/api/v1/thing")
    async def thing(body: Body) -> dict[str, int]:
        return {"amount": body.amount}

    return TestClient(app)


def test_a_rejected_body_is_logged_by_field_path_without_its_values(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="bfx.security"):
        response = _client().post("/api/v1/thing", json={"amount": "SECRET-VALUE"},
                                  headers={LOGIN_HEADER: "will@github"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "amount"]  # FastAPI's own body
    (record,) = [r for r in caplog.records if r.name == "bfx.security"]
    assert (record.event, record.reason, record.login) == ("input_rejected", "request_validation",
                                                           "will@github")
    assert record.fields == ["body.amount", "body.note"] and record.path == "/api/v1/thing"
    assert "SECRET-VALUE" not in caplog.text


def test_a_valid_body_logs_nothing(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="bfx.security"):
        assert _client().post("/api/v1/thing", json={"amount": 1, "note": "x"}).status_code == 200
    assert [r for r in caplog.records if r.name == "bfx.security"] == []


def test_the_web_api_installs_it() -> None:
    from fastapi.exceptions import RequestValidationError

    from bfx_funding_bot.apps.webapi import app

    # FastAPI always has a default handler for this class; it must be ours.
    handler = app.exception_handlers[RequestValidationError]
    assert "install_request_validation_log" in handler.__qualname__
