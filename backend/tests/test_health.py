import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from bfx_funding_bot.apps.webapi import app
from bfx_funding_bot.core.authority import AuthorityMismatch


def test_health_returns_200() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def _authority_db(path: Path, epoch: str) -> str:
    """A database at ``epoch``: the seeded legacy row, a switch, none, or no table."""
    url = f"sqlite+aiosqlite:///{path}"

    async def build() -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        from bfx_funding_bot.modules.ledger.tables import CapitalAuthorityEpochRow

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            if epoch != "drop":
                await conn.run_sync(CapitalAuthorityEpochRow.__table__.create)
            rows = {"legacy": ["legacy"], "ledger": ["legacy", "ledger"]}.get(epoch, [])
            for seq, authority in enumerate(rows, start=1):
                await conn.execute(
                    text(
                        "INSERT INTO capital_authority_epoch "
                        "(epoch_seq, authority, set_at_ms, actor, reason) "
                        "VALUES (:seq, :authority, 1, 'test', 'test')"
                    ),
                    {"seq": seq, "authority": authority},
                )
        await engine.dispose()

    asyncio.run(build())
    return url


def test_startup_reads_the_supported_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", _authority_db(tmp_path / "ledger.db", "ledger"))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.session_factory is not None


@pytest.mark.parametrize(
    ("epoch", "match"),
    [
        ("drop", "authority_missing table"),
        ("empty", "authority_missing row"),
        # A database never switched (or restored from before the switch): only the ledger runs.
        ("legacy", "authority_unsupported value=legacy"),
    ],
)
def test_startup_refuses_an_unreadable_or_unsupported_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, epoch: str, match: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", _authority_db(tmp_path / f"{epoch}.db", epoch))
    with pytest.raises(AuthorityMismatch, match=match), TestClient(app):
        pass
