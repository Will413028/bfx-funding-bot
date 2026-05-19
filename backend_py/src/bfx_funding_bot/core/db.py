from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from bfx_funding_bot.core.settings import Settings


class Base(DeclarativeBase):
    """Single SQLAlchemy declarative base. ORM tables across all modules
    inherit from this so Alembic autogenerate sees one unified metadata.

    Tables are defined in modules/<feature>/tables.py. To register them
    with Alembic, alembic/env.py imports each module's tables file
    (their import side-effects populate Base.metadata).
    """


def _prepare_engine_kwargs(raw_url: str) -> dict[str, object]:
    """Convert a plain postgresql:// URL (Go/psycopg2-style) to asyncpg kwargs.

    Transforms:
    - scheme postgresql:// → postgresql+asyncpg://
    - host: strip `-pooler` suffix (asyncpg prepared stmt vs PgBouncer
      transaction-mode incompatibility)
    - sslmode: extracted to SSL context in connect_args
    - channel_binding: removed (asyncpg does not support)
    """
    from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

    from bfx_funding_bot.core.settings import _strip_pooler_from_host

    # Normalise scheme.
    url = raw_url
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]

    # Parse URL parts.
    parts = urlsplit(url)
    qs = parse_qs(parts.query, keep_blank_values=True)

    # Detect SSL requirement.
    sslmode = qs.pop("sslmode", ["prefer"])[0]
    qs.pop("channel_binding", None)  # asyncpg does not accept this

    # Strip -pooler from host.
    new_host = _strip_pooler_from_host(parts.hostname)
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += f":{parts.password}"
        userinfo += "@"
    port_suffix = f":{parts.port}" if parts.port is not None else ""
    netloc = f"{userinfo}{new_host or ''}{port_suffix}"

    new_query = urlencode({k: v[0] for k, v in qs.items()})
    clean_url = urlunsplit((parts.scheme, netloc, parts.path, new_query, parts.fragment))

    connect_args: dict[str, object] = {}
    if sslmode in ("require", "verify-ca", "verify-full"):
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        if sslmode == "require":
            # Matches psycopg2 semantics: encrypt the channel but do not verify
            # the server cert. Adequate for personal-use Neon connections; for
            # production / multi-tenant deployments switch the .env DATABASE_URL
            # to sslmode=verify-full so the cert chain is validated.
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        connect_args["ssl"] = ctx

    return {"url": clean_url, "connect_args": connect_args}


def make_engine(settings: Settings) -> AsyncEngine:
    """Create async engine with serverless-Postgres-friendly pool config.

    pool_pre_ping=True: health-check connection before handing to caller.
    pool_recycle=600 (10 min): force recycle below Neon's ~15min idle cut.
    Combined with daemon-level keepalive task (every 5min SELECT 1)
    in core.keepalive (Task 7).
    """
    kwargs = _prepare_engine_kwargs(settings.database_url)
    return create_async_engine(
        str(kwargs["url"]),
        connect_args=kwargs["connect_args"],
        echo=False,
        pool_pre_ping=True,
        pool_recycle=600,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
