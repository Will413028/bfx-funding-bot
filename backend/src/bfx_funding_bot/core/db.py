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
    """Convert a libpq postgresql:// URL to asyncpg kwargs.

    - scheme postgresql:// → postgresql+asyncpg://
    - sslmode (checked by `database_sslmode`) and sslrootcert become an SSL context, or
      ``ssl=False``, in connect_args; asyncpg takes neither as a URL parameter
    """
    import ssl
    from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

    from bfx_funding_bot.core.settings import database_sslmode

    url = raw_url
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]

    parts = urlsplit(url)
    qs = parse_qs(parts.query, keep_blank_values=True)
    sslmode = database_sslmode(qs)
    qs.pop("sslmode", None)
    rootcert = qs.pop("sslrootcert", [None])[0]
    clean_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode({k: v[0] for k, v in qs.items()}),
         parts.fragment)
    )

    connect_args: dict[str, object] = {}
    if sslmode == "disable":
        connect_args["ssl"] = False
    else:
        # libpq semantics: both verify the chain; only verify-full also checks the host name.
        ctx = ssl.create_default_context(cafile=rootcert)
        ctx.check_hostname = sslmode == "verify-full"
        connect_args["ssl"] = ctx

    return {"url": clean_url, "connect_args": connect_args}


def make_async_engine_from_url(raw_url: str) -> AsyncEngine:
    """Async engine + serverless-Postgres pool config + URL transform.

    Single source of truth shared by `make_engine` (Settings-driven) and
    `marketfeed.daemon.build_daemon` (MarketfeedConfig-driven). Both paths
    now apply `_prepare_engine_kwargs` (D2 URL transform: scheme rewrite,
    sslmode checked and lifted into connect_args) and the D3 pool config (`pool_pre_ping=True`, `pool_recycle=600`).

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
