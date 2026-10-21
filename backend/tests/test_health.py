import asyncio
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from bfx_funding_bot.apps.webapi import app
from bfx_funding_bot.core.authority import AuthorityMismatch
from bfx_funding_bot.core.schema_head import SchemaHeadMismatch, build_head


def _authority_db(path: Path, epoch: str, *, heads: tuple[str, ...] | None = None) -> str:
    """A database at ``epoch``: the seeded legacy row, a switch, none, or no table.

    Stamped at this build's schema head unless ``heads`` names other revisions (an
    empty tuple: never migrated, no ``alembic_version`` table at all).
    """
    url = f"sqlite+aiosqlite:///{path}"
    stamped = (build_head(),) if heads is None else heads

    async def build() -> None:
        from sqlalchemy.ext.asyncio import create_async_engine

        from bfx_funding_bot.modules.ledger.tables import CapitalAuthorityEpochRow

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            if stamped:
                await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            for revision in stamped:
                await conn.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                    {"rev": revision},
                )
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
    # Never the ambient DATABASE_URL: a developer's .env names a real database.
    monkeypatch.setenv("DATABASE_URL", _authority_db(tmp_path / "ledger.db", "ledger"))
    with TestClient(app) as client:
        resp = client.get("/health")
        assert (resp.status_code, resp.json()) == (200, {"status": "ok"})
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


def test_startup_refuses_the_daemon_halt_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """9d-2: the web API holds the read token only; a left-over halt token fails the boot,
    so the deploy rolls back instead of quietly keeping the web API able to halt."""
    monkeypatch.setenv("DATABASE_URL", _authority_db(tmp_path / "ledger.db", "ledger"))
    monkeypatch.setenv("BFX_ADMIN_TOKEN", "halt-secret")
    with pytest.raises(RuntimeError, match="bfx_admin_token_in_webapi_env"), TestClient(app):
        pass


@pytest.mark.parametrize(
    ("heads", "database"),
    [
        # A rollback onto a newer schema, or an old web API left on a migrated one.
        (("ffffffffffff",), "ffffffffffff"),
        ((), "none"),  # never migrated
    ],
)
def test_startup_refuses_a_database_at_another_schema_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, heads: tuple[str, ...], database: str
) -> None:
    """ADR 2026-10-15 D6: the web API, like the bot, refuses to boot unless the database is
    at this image's single migration head, so uvicorn exits instead of serving a schema
    it was not written for (the epoch is valid: only the schema refuses)."""
    monkeypatch.setenv(
        "DATABASE_URL", _authority_db(tmp_path / "other.db", "ledger", heads=heads))
    match = f"schema_head_mismatch database={database} build={build_head()}"
    with pytest.raises(SchemaHeadMismatch, match=match), TestClient(app):
        pass
    assert getattr(app.state, "read_models", None) is None


def _boot_refusals(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(r.event, r.reason) for r in caplog.records
            if r.name == "bfx.security" and r.levelno == logging.CRITICAL]


@pytest.mark.parametrize(("setup", "error", "reason"), [
    pytest.param({"BFX_ADMIN_TOKEN": "halt-secret"}, RuntimeError, "admin_token_in_webapi_env",
                 id="halt-token"),
    pytest.param({"BFX_WEBAPI_ALLOWED_HOSTS": None}, Exception, "ingress_settings_invalid",
                 id="ingress"),
    pytest.param({"heads": ("ffffffffffff",)}, SchemaHeadMismatch, "schema_head_mismatch",
                 id="schema-head"),
    pytest.param({"epoch": "legacy"}, AuthorityMismatch, "capital_authority_unreadable",
                 id="authority"),
])
def test_each_boot_refusal_is_a_critical_security_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    setup: dict[str, object], error: type[Exception], reason: str,
) -> None:
    """9j: ``boot_refused`` with a stable reason code; the halt token's value never logged."""
    env = {k: v for k, v in setup.items() if k.startswith("BFX_")}
    heads = setup.get("heads")
    monkeypatch.setenv("DATABASE_URL", _authority_db(
        tmp_path / "db.db", str(setup.get("epoch", "ledger")),
        heads=heads if isinstance(heads, tuple) else None))
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key)
        else:
            monkeypatch.setenv(key, str(value))

    with caplog.at_level(logging.WARNING), pytest.raises(error), TestClient(app):
        pass

    assert _boot_refusals(caplog) == [("boot_refused", reason)]
    assert "halt-secret" not in caplog.text
