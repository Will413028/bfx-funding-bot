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


class TestMakeAsyncEngineFromUrl:
    """5/21 chaos regression — daemon.py:614 bypassed _prepare_engine_kwargs
    + D3 pool config since Phase 4.1 (`26b059d`). Helper unifies the asyncpg
    engine construction path so daemon + make_engine share one source of truth.
    """

    def test_handles_raw_neon_libpq_url(self):
        """Raw Neon dashboard URL: scheme rewritten, sslmode + channel_binding
        stripped, -pooler suffix removed, pool config applied."""
        from bfx_funding_bot.core.db import make_async_engine_from_url
        url = (
            "postgresql://u:p@ep-foo-pooler.ap-southeast-1.aws.neon.tech/db"
            "?sslmode=require&channel_binding=require"
        )
        engine = make_async_engine_from_url(url)
        u = str(engine.url)
        assert u.startswith("postgresql+asyncpg://")
        assert "sslmode" not in u
        assert "channel_binding" not in u
        assert "-pooler." not in u
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == 600

    def test_handles_asyncpg_scheme_with_libpq_query(self):
        """Phase 4.1 partially-transformed URL: asyncpg scheme already set
        but query still carries libpq sslmode/channel_binding. Helper still
        strips them so asyncpg connect() doesn't crash on unknown kwargs."""
        from bfx_funding_bot.core.db import make_async_engine_from_url
        url = (
            "postgresql+asyncpg://u:p@ep-foo.region.aws.neon.tech/db"
            "?sslmode=require&channel_binding=require"
        )
        engine = make_async_engine_from_url(url)
        u = str(engine.url)
        assert "sslmode" not in u
        assert "channel_binding" not in u
