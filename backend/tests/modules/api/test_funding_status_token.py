"""The web API reads the daemon's status with the read token only (9d-2)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from bfx_funding_bot.modules.api import funding_status


def test_reads_with_the_read_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(funding_status.httpx, "AsyncClient", client)
    monkeypatch.setenv("BFX_ADMIN_READ_TOKEN", "read-only")
    monkeypatch.setenv("BFX_ADMIN_TOKEN", "halt-secret")
    assert asyncio.run(funding_status._read_daemon("/admin/trading-status")) == {"ok": True}
    assert seen == ["Bearer read-only"]


def test_the_halt_token_alone_is_not_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_ADMIN_READ_TOKEN", raising=False)
    monkeypatch.setenv("BFX_ADMIN_TOKEN", "halt-secret")
    with pytest.raises(RuntimeError, match="daemon_status_unconfigured"):
        asyncio.run(funding_status._read_daemon("/admin/trading-status"))


def test_reads_with_the_read_token_file_not_the_env(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path) -> None:
    """9h: with BFX_SECRETS_DIR the token is the web API's secret file; a stale env value
    is not used, and a missing file never falls back to it."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient

    def client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(funding_status.httpx, "AsyncClient", client)
    (tmp_path / "BFX_ADMIN_READ_TOKEN").write_text("file-read-token\n")
    monkeypatch.setenv("BFX_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("BFX_ADMIN_READ_TOKEN", "stale-env-token")
    assert asyncio.run(funding_status._read_daemon("/admin/trading-status")) == {"ok": True}
    assert seen == ["Bearer file-read-token"]
    (tmp_path / "BFX_ADMIN_READ_TOKEN").unlink()
    with pytest.raises(ValueError, match="BFX_ADMIN_READ_TOKEN is missing"):
        asyncio.run(funding_status._read_daemon("/admin/trading-status"))
    assert seen == ["Bearer file-read-token"]
