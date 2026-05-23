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

    def test_sslmode_preserved(self):
        s = _s("postgresql://u:p@host.example/db?sslmode=require")
        assert "sslmode=require" in s.database_url_sync

    def test_channel_binding_preserved(self):
        s = _s("postgresql://u:p@host.example/db?sslmode=require&channel_binding=require")
        assert "channel_binding=require" in s.database_url_sync

    def test_pooler_suffix_stripped(self):
        """Neon -pooler endpoint stripped — alembic must use direct endpoint."""
        s = _s(
            "postgresql://u:p@ep-foo-bar-123-pooler.ap-southeast-1.aws.neon.tech/db"
            "?sslmode=require"
        )
        out = s.database_url_sync
        assert "-pooler." not in out
        assert "ep-foo-bar-123.ap-southeast-1.aws.neon.tech" in out

    def test_no_pooler_no_change(self):
        """Already-direct endpoint unchanged."""
        s = _s(
            "postgresql://u:p@ep-foo-bar-123.ap-southeast-1.aws.neon.tech/db"
            "?sslmode=require"
        )
        out = s.database_url_sync
        assert "ep-foo-bar-123.ap-southeast-1.aws.neon.tech" in out

    def test_ssl_translated_to_sslmode(self):
        """Phase 4.1 asyncpg-pre-transformed URL has `ssl=require` — psycopg
        needs `sslmode=require`. Accessor translates."""
        s = _s("postgresql+asyncpg://u:p@host.example/db?ssl=require")
        out = s.database_url_sync
        assert "ssl=require" not in out
        assert "sslmode=require" in out

    def test_asyncpg_scheme_replaced_by_psycopg(self):
        """Phase 4.1 asyncpg URL should also become psycopg for alembic."""
        s = _s("postgresql+asyncpg://u:p@host.example/db?ssl=require")
        assert s.database_url_sync.startswith("postgresql+psycopg://")
        assert "+asyncpg" not in s.database_url_sync
