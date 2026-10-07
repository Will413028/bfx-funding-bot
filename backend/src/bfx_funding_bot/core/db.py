from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import CheckConstraint, Column, Table, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import JSON

from bfx_funding_bot.core.settings import Settings


class Base(DeclarativeBase):
    """Single SQLAlchemy declarative base. ORM tables across all modules
    inherit from this so Alembic autogenerate sees one unified metadata.

    Tables are defined in modules/<feature>/tables.py. To register them
    with Alembic, alembic/env.py imports each module's tables file
    (their import side-effects populate Base.metadata).
    """


# The one JSON column type: Python ``None`` is SQL NULL, never the JSON literal ``null`` (the
# SQLAlchemy default, which ``IS NULL`` does not see). ``with_variant`` does not carry
# ``none_as_null`` over, so both variants set it; tests/core/test_json_columns.py pins every
# JSON column to it.
JSON_DOCUMENT = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


@event.listens_for(Column, "after_parent_attach")
def _refuse_the_json_literal(column: Column[object], table: object) -> None:
    """Every written ``JSON_DOCUMENT`` column of ``public`` also refuses a stored JSON ``null``
    (raw SQL can still write one): ``ck_<table>_<column>_json``, added by a6c7e8f9b0d1. Not a
    table of another schema nor a generated column. PostgreSQL only; sqlite has no
    ``jsonb_typeof``."""
    if column.type is not JSON_DOCUMENT or column.computed is not None:
        return
    assert isinstance(table, Table)
    if table.schema is not None:
        return
    table.append_constraint(CheckConstraint(
        f"jsonb_typeof({column.name}) <> 'null'",
        name=f"ck_{table.name}_{column.name}_json",
    ).ddl_if(dialect="postgresql"))


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


def make_async_engine_from_url(raw_url: str) -> AsyncEngine:
    """Async engine + serverless-Postgres pool config + URL transform.

    Single source of truth shared by `make_engine` (Settings-driven) and
    `marketfeed.daemon.build_daemon` (MarketfeedConfig-driven). Both paths
    now apply `_prepare_engine_kwargs` (D2 URL transform: scheme rewrite,
    sslmode/channel_binding stripping, -pooler removal, SSL context lift)
    and the D3 pool config (`pool_pre_ping=True`, `pool_recycle=600`).

    Before unification daemon.py:614 was a raw `create_async_engine(url)`
    call from Phase 4.1 (`26b059d`); a DATABASE_URL in libpq form (with
    `sslmode`) crashed asyncpg at connect time with `TypeError(sslmode)`.

    Non-Postgres URLs (e.g. `sqlite+aiosqlite://` used by tests) bypass the
    transform — `_prepare_engine_kwargs` is Postgres-specific and would
    corrupt a sqlite URL's netloc/path round-trip via urlunsplit.
    """
    is_postgres = (
        raw_url.startswith("postgresql://")
        or raw_url.startswith("postgres://")
        or raw_url.startswith("postgresql+")
    )
    if is_postgres:
        kwargs = _prepare_engine_kwargs(raw_url)
        return create_async_engine(
            str(kwargs["url"]),
            connect_args=kwargs["connect_args"],
            echo=False,
            pool_pre_ping=True,
            pool_recycle=600,
        )
    return create_async_engine(raw_url, echo=False)


def make_engine(settings: Settings) -> AsyncEngine:
    return make_async_engine_from_url(settings.database_url)


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
