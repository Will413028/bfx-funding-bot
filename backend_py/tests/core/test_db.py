"""Tests for db._prepare_engine_kwargs — D2 async URL transform."""
from bfx_funding_bot.core.db import _prepare_engine_kwargs


class TestAsyncUrlTransform:
    def test_scheme_rewritten(self):
        kw = _prepare_engine_kwargs("postgresql://u:p@h/db?sslmode=require")
        assert str(kw["url"]).startswith("postgresql+asyncpg://")

    def test_sslmode_removed_from_url(self):
        kw = _prepare_engine_kwargs("postgresql://u:p@h/db?sslmode=require")
        assert "sslmode" not in str(kw["url"])
        assert "ssl" in kw["connect_args"]  # SSL context lives in connect_args

    def test_channel_binding_removed(self):
        kw = _prepare_engine_kwargs(
            "postgresql://u:p@h/db?sslmode=require&channel_binding=require"
        )
        assert "channel_binding" not in str(kw["url"])

    def test_pooler_suffix_stripped(self):
        """Neon -pooler endpoint stripped — asyncpg prepared stmt vs PgBouncer."""
        kw = _prepare_engine_kwargs(
            "postgresql://u:p@ep-foo-123-pooler.ap-southeast-1.aws.neon.tech/db"
            "?sslmode=require"
        )
        url = str(kw["url"])
        assert "-pooler." not in url
        assert "ep-foo-123.ap-southeast-1.aws.neon.tech" in url

    def test_no_pooler_no_change_to_host(self):
        kw = _prepare_engine_kwargs(
            "postgresql://u:p@ep-foo-123.ap-southeast-1.aws.neon.tech/db?sslmode=require"
        )
        assert "ep-foo-123.ap-southeast-1.aws.neon.tech" in str(kw["url"])


class TestEnginePoolConfig:
    def test_engine_has_pool_pre_ping_enabled(self):
        from bfx_funding_bot.core.db import make_engine
        from bfx_funding_bot.core.settings import Settings
        s = Settings.model_construct(database_url="postgresql://u:p@h/db")
        engine = make_engine(s)
        assert engine.pool._pre_ping is True

    def test_engine_has_pool_recycle_set(self):
        from bfx_funding_bot.core.db import make_engine
        from bfx_funding_bot.core.settings import Settings
        s = Settings.model_construct(database_url="postgresql://u:p@h/db")
        engine = make_engine(s)
        assert engine.pool._recycle == 600
