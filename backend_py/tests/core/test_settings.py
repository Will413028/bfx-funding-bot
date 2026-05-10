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
