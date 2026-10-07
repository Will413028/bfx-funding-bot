import pytest

from bfx_funding_bot.core.db import _prepare_engine_kwargs
from bfx_funding_bot.core.settings import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.setenv("BITFINEX_API_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    s = Settings()

    assert s.database_url == "postgresql+asyncpg://u:p@h/d"
    assert s.bitfinex_api_base_url == "https://api.example.com"
    assert s.log_level == "DEBUG"


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/d")
    monkeypatch.delenv("BITFINEX_API_BASE_URL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    s = Settings()

    assert s.bitfinex_api_base_url == "https://api-pub.bitfinex.com"
    assert s.log_level == "INFO"


def _s(url: str) -> Settings:
    """Build Settings with given DATABASE_URL, ignoring .env."""
    return Settings.model_construct(database_url=url)


class TestDatabaseUrlSync:
    """database_url_sync: for alembic (psycopg driver)."""

    def test_scheme_rewritten_to_psycopg(self):
        s = _s("postgresql://u:p@host.example/db")
        assert s.database_url_sync.startswith("postgresql+psycopg://")

    def test_legacy_postgres_scheme_also_handled(self):
        s = _s("postgres://u:p@host.example/db")
        assert s.database_url_sync.startswith("postgresql+psycopg://")

    def test_an_asyncpg_scheme_is_rewritten_to_psycopg(self):
        s = _s("postgresql+asyncpg://u:p@host.example/db")
        assert s.database_url_sync.startswith("postgresql+psycopg://")


class TestDatabaseSslmode:
    """Both drivers read the TLS mode through ``database_sslmode``: only modes that verify the
    server, or an explicit plaintext one. Mutation: accept ``require`` again
    (``test_a_mode_that_does_not_verify_is_refused``)."""

    @pytest.mark.parametrize("mode", ["require", "prefer", "allow", "no-such-mode"])
    def test_a_mode_that_does_not_verify_is_refused(self, mode: str) -> None:
        url = f"postgresql://u:p@host.example/db?sslmode={mode}"
        with pytest.raises(ValueError, match=f"sslmode={mode}"):
            _ = _s(url).database_url_sync
        with pytest.raises(ValueError, match=f"sslmode={mode}"):
            _prepare_engine_kwargs(url)

    def test_asyncpgs_own_ssl_parameter_is_refused(self) -> None:
        url = "postgresql+asyncpg://u:p@host.example/db?ssl=require"
        with pytest.raises(ValueError, match="sslmode=, not ssl="):
            _ = _s(url).database_url_sync
        with pytest.raises(ValueError, match="sslmode=, not ssl="):
            _prepare_engine_kwargs(url)

    @pytest.mark.parametrize(
        ("query", "mode"),
        [("", "disable"), ("?sslmode=disable", "disable"),
         ("?sslmode=verify-ca", "verify-ca"), ("?sslmode=verify-full", "verify-full")],
    )
    def test_the_sync_url_always_names_the_mode(self, query: str, mode: str) -> None:
        url = _s(f"postgresql://u:p@host.example:5432/db{query}").database_url_sync
        assert url == f"postgresql+psycopg://u:p@host.example:5432/db?sslmode={mode}"
