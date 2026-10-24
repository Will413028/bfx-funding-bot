"""The web API's tailnet ingress (9d-5): Host allowlist, same-origin JSON for state changes,
the static UI with its hash-based CSP, and the /fe-events Sentry tunnel.

Mutations these catch: dropping the Origin check, the Sec-Fetch-Site check, the JSON check
or the Host check; a CSP that loses the inline-script hashes; /fe-events forwarding to
another project, accepting a cross-site POST, a caller without the operator capability or
an unbounded burst; an API response without its sandbox CSP or with a cacheable body.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pytest_httpx import HTTPXMock
from starlette.requests import ClientDisconnect

from bfx_funding_bot.core.auth import LOGIN_HEADER
from bfx_funding_bot.core.log_format import JsonFormatter
from bfx_funding_bot.modules.api import ingress
from bfx_funding_bot.modules.api.ingress import (
    FE_EVENTS_PATH,
    MAX_EVENT_BYTES,
    content_security_policy,
    install_ingress,
    sentry_envelope_target,
)
from tests.ingress_support import (
    ALLOWED_HOST,
    INGRESS_ENV,
    attach_ingress,
    ingress_settings,
    operator_headers,
)

ORIGIN = f"https://{ALLOWED_HOST}"
SCRIPT = b"self.__next_f.push([1,\"x\"])"
SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT).digest()).decode()
DSN = INGRESS_ENV["BFX_FE_SENTRY_DSN"]
ENVELOPE = b'{"dsn":"' + DSN.encode() + b'"}\n{"type":"event"}\n{}'


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    page = tmp_path / "en" / "overview" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"<html><head><script src=\"/_next/x.js\"></script></head><body>"
                     b"<script>" + SCRIPT + b"</script></body></html>")
    (tmp_path / "404.html").write_text("<html>404</html>")
    return tmp_path


@pytest.fixture
def client(export_dir: Path) -> TestClient:
    app = attach_ingress(FastAPI())

    @app.post("/api/v1/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.delete("/api/v1/thing")
    async def drop_thing() -> dict[str, str]:
        return {"dropped": "yes"}

    @app.get("/api/v1/getonly")
    async def getonly() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    install_ingress(app, static_dir=export_dir)
    return TestClient(app, base_url=f"https://{ALLOWED_HOST}")


def _post(client: TestClient, path: str = "/api/v1/thing", **headers: str):
    base = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
    base.update({k.replace("_", "-"): v for k, v in headers.items()})
    return client.post(path, content=b"{}", headers={k: v for k, v in base.items() if v != ""})


def test_a_same_origin_json_post_reaches_the_handler(client: TestClient) -> None:
    response = _post(client)
    assert response.status_code == 200 and response.json() == {"ok": "yes"}
    assert response.headers["content-security-policy"] == ingress.API_CSP  # an API answer
    assert response.headers["x-frame-options"] == "DENY"


def test_a_host_outside_the_allowlist_is_refused(client: TestClient) -> None:
    response = client.get("/en/overview/", headers={"Host": "evil.example"})
    assert response.status_code == 400 and response.json()["detail"] == "host_not_allowed"


def test_the_probes_answer_any_host(client: TestClient) -> None:
    """bfx-deploy probes 127.0.0.1:8000 from inside the container."""
    assert client.get("/health", headers={"Host": "127.0.0.1:8000"}).status_code == 200


@pytest.mark.parametrize("headers", [
    pytest.param({"Sec_Fetch_Site": "cross-site"}, id="sec-fetch-site-cross-site"),
    pytest.param({"Sec_Fetch_Site": "same-site"}, id="sec-fetch-site-same-site"),
    pytest.param({"Origin": "https://evil.example"}, id="foreign-origin"),
    pytest.param({"Origin": f"http://{ALLOWED_HOST}"}, id="plain-http-origin"),
    pytest.param({"Origin": ""}, id="no-origin"),
])
def test_a_cross_site_post_is_refused(client: TestClient, headers: dict[str, str]) -> None:
    response = _post(client, **headers)
    assert response.status_code == 403 and response.json()["detail"] == "cross_site_refused"


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded",
                                          "multipart/form-data; boundary=x", ""])
def test_a_post_without_json_is_refused(client: TestClient, content_type: str) -> None:
    response = _post(client, Content_Type=content_type)
    assert response.status_code == 415 and response.json()["detail"] == "json_required"


def test_the_static_ui_carries_the_hash_csp_and_the_security_headers(client: TestClient) -> None:
    response = client.get("/en/overview/")
    assert response.status_code == 200 and SCRIPT in response.content
    policy = response.headers["content-security-policy"]
    assert f"'sha256-{SCRIPT_HASH}'" in policy and "'unsafe-inline'" not in policy.split("script-src", 1)[1].split(";")[0]
    assert "frame-ancestors 'none'" in policy and "base-uri 'none'" in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "cache-control" not in response.headers or "no-store" not in response.headers["cache-control"]


def test_api_answers_render_nothing_and_are_never_stored(client: TestClient) -> None:
    for response in (client.get("/api/v1/getonly"), _post(client), client.get("/api/v1/nope")):
        assert response.headers["content-security-policy"] == ingress.API_CSP
        assert response.headers.get_list("cache-control") == ["no-store"]
    fe = client.post(FE_EVENTS_PATH, content=ENVELOPE, headers={"Origin": ORIGIN})
    assert fe.headers["content-security-policy"] == ingress.API_CSP


def test_an_api_handlers_own_cache_header_is_replaced(export_dir: Path) -> None:
    app = attach_ingress(FastAPI())

    @app.get("/api/v1/cached")
    async def cached() -> Any:
        from fastapi.responses import JSONResponse
        return JSONResponse({}, headers={"Cache-Control": "public, max-age=3600"})

    install_ingress(app, static_dir=export_dir)
    response = TestClient(app, base_url=f"https://{ALLOWED_HOST}").get("/api/v1/cached")
    assert response.headers.get_list("cache-control") == ["no-store"]


def test_root_redirects_to_the_overview(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/en/overview/"


def test_an_unknown_page_is_the_export_404(client: TestClient) -> None:
    response = client.get("/en/nope/")
    assert response.status_code == 404 and b"404" in response.content


def test_the_policy_lists_exactly_the_given_hashes() -> None:
    policy = content_security_policy(frozenset({"abc", "def"}))
    assert "script-src 'self' 'sha256-abc' 'sha256-def';" in policy


def test_fe_events_forward_to_the_configured_project_only(client: TestClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://o1.ingest.sentry.io/api/42/envelope/", status_code=200)
    response = client.post(FE_EVENTS_PATH, content=ENVELOPE,
                           headers={**operator_headers(), "Origin": ORIGIN, "Content-Type": "text/plain;charset=UTF-8"})
    assert response.status_code == 200
    (forwarded,) = httpx_mock.get_requests()
    assert forwarded.content == ENVELOPE


@pytest.mark.parametrize("dsn", [
    "https://publickey@o1.ingest.sentry.io/43",          # another project
    "https://publickey@evil.example/42",                 # another host
    "https://otherkey@o1.ingest.sentry.io/42",           # another key
])
def test_fe_events_refuse_another_dsn(client: TestClient, dsn: str) -> None:
    body = b'{"dsn":"' + dsn.encode() + b'"}\n{}'
    response = client.post(FE_EVENTS_PATH, content=body, headers={**operator_headers(), "Origin": ORIGIN})
    assert response.status_code == 400 and response.json()["detail"] == "dsn_not_allowed"


@pytest.mark.parametrize("headers", [{}, operator_headers(user_id="someone-else")])
def test_fe_events_need_the_operator_capability(
    client: TestClient, httpx_mock: HTTPXMock, headers: dict[str, str],
) -> None:
    response = client.post(FE_EVENTS_PATH, content=ENVELOPE, headers={**headers, "Origin": ORIGIN})
    assert response.status_code == 401 and httpx_mock.get_requests() == []


def test_fe_events_have_their_own_bucket(
    export_dir: Path, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = ingress.TokenBucketLimiter
    monkeypatch.setattr(ingress, "TokenBucketLimiter",
                        lambda **kw: frozen(**kw, clock=lambda: 1000.0))  # no refill mid-test
    app = attach_ingress(FastAPI())
    install_ingress(app, static_dir=export_dir)
    client = TestClient(app, base_url=f"https://{ALLOWED_HOST}")
    httpx_mock.add_response(url="https://o1.ingest.sentry.io/api/42/envelope/", status_code=200,
                            is_reusable=True)
    headers = {**operator_headers(), "Origin": ORIGIN}
    codes = [client.post(FE_EVENTS_PATH, content=ENVELOPE, headers=headers).status_code
             for _ in range(ingress.FE_EVENTS_PER_MIN + 1)]
    assert codes[:-1] == [200] * ingress.FE_EVENTS_PER_MIN and codes[-1] == 429
    assert len(httpx_mock.get_requests()) == ingress.FE_EVENTS_PER_MIN


def test_fe_events_refuse_a_cross_site_post(client: TestClient) -> None:
    response = client.post(FE_EVENTS_PATH, content=ENVELOPE, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_fe_events_refuse_an_oversized_envelope(client: TestClient) -> None:
    response = client.post(FE_EVENTS_PATH, content=b"x" * (MAX_EVENT_BYTES + 1),
                           headers={**operator_headers(), "Origin": ORIGIN})
    assert response.status_code == 413


def test_an_envelope_without_a_dsn_header_has_no_target() -> None:
    assert sentry_envelope_target(b"garbage\n{}", DSN) is None
    assert sentry_envelope_target(b'{"event_id":"x"}\n{}', DSN) is None


def test_a_bodiless_same_origin_delete_needs_no_content_type(client: TestClient) -> None:
    """The UI's DELETE carries no body and so no Content-Type; the Origin check guards it."""
    response = client.delete("/api/v1/thing", headers={"Origin": ORIGIN})
    assert response.status_code == 200 and response.json() == {"dropped": "yes"}


def test_a_delete_with_a_non_json_body_is_refused(client: TestClient) -> None:
    response = client.request("DELETE", "/api/v1/thing", content=b"a=1",
                              headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 415


def test_a_bodiless_cross_site_delete_is_refused(client: TestClient) -> None:
    assert client.delete("/api/v1/thing", headers={"Origin": "https://evil.example"}).status_code == 403


def test_an_unknown_api_path_answers_json_not_the_ui(client: TestClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404 and response.json() == {"detail": "not_found"}


def test_a_dsn_with_a_path_prefix_and_port_targets_its_own_endpoint() -> None:
    dsn = "https://key@sentry.example:9000/prefix/42"
    envelope = b'{"dsn":"' + dsn.encode() + b'"}\n{}'
    assert sentry_envelope_target(envelope, dsn) == "https://sentry.example:9000/prefix/api/42/envelope/"
    other_port = b'{"dsn":"https://key@sentry.example:9001/prefix/42"}\n{}'
    assert sentry_envelope_target(other_port, dsn) is None


def test_a_known_api_path_with_another_method_answers_405(client: TestClient) -> None:
    """The fallback must not turn a wrong method on a real route into 404."""
    response = _post(client, "/api/v1/getonly")
    assert response.status_code == 405 and response.json() == {"detail": "method_not_allowed"}


@pytest.mark.parametrize("dsn", [
    "https://key@sentry.example:abc/42",
    "https://key@sentry.example:99999/42",
])
def test_a_configured_dsn_with_an_invalid_port_refuses_to_start(dsn: str) -> None:
    with pytest.raises(ValidationError, match="invalid port"):
        ingress_settings(BFX_FE_SENTRY_DSN=dsn)


def test_fe_events_answer_a_client_disconnect_without_an_error(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def disconnected(request: object, limit: int) -> bytes:
        raise ClientDisconnect

    monkeypatch.setattr(ingress, "_read_limited", disconnected)
    response = client.post(FE_EVENTS_PATH, content=ENVELOPE, headers={**operator_headers(), "Origin": ORIGIN})
    assert response.status_code == 400 and response.json()["detail"] == "client_disconnected"


# ------------------------------------------------------- security log (9j)


@pytest.mark.parametrize(("send", "status", "reason", "header", "value"), [
    pytest.param(lambda c: c.get("/en/overview/", headers={"Host": "evil.example\r\nX: y",
                                                           LOGIN_HEADER: "will@github"}),
                 400, "host_not_allowed", "host", "evil.example\r\nX: y", id="host"),
    pytest.param(lambda c: _post(c, Sec_Fetch_Site="cross-site", **{LOGIN_HEADER: "will@github"}),
                 403, "cross_site_refused", "sec-fetch-site", "cross-site", id="sec-fetch-site"),
    pytest.param(lambda c: _post(c, Origin="https://evil.example", **{LOGIN_HEADER: "will@github"}),
                 403, "cross_site_refused", "origin", "https://evil.example", id="origin"),
    pytest.param(lambda c: _post(c, Content_Type="text/plain", **{LOGIN_HEADER: "will@github"}),
                 415, "json_required", "content-type", "text/plain", id="content-type"),
])
def test_each_refusal_is_logged_with_the_header_that_failed_and_the_login(
    client: TestClient, caplog: pytest.LogCaptureFixture,
    send: Callable[[TestClient], Any], status: int, reason: str, header: str, value: str,
) -> None:
    with caplog.at_level(logging.WARNING, logger="bfx.security"):
        response = send(client)

    assert response.status_code == status
    [record] = [r for r in caplog.records if r.name == "bfx.security"]
    assert (record.event, record.reason, record.status) == ("request_rejected", reason, status)
    assert (record.header, record.value, record.login) == (header, value, "will@github")
    line = JsonFormatter("webapi").format(record)
    assert line.splitlines() == [line]


def test_an_accepted_request_logs_no_refusal(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="bfx.security"):
        assert _post(client).status_code == 200

    assert [r for r in caplog.records if r.name == "bfx.security"] == []
