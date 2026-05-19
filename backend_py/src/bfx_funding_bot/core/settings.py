"""Application settings.

DATABASE_URL accepts Neon dashboard libpq-style URL (e.g.
`postgresql://u:p@ep-xxx-pooler.<region>.aws.neon.tech/db?sslmode=require&channel_binding=require`).
Two accessors transform it for the two SQLAlchemy drivers we use:

- `database_url_sync` — psycopg, for alembic env.py
- `database_url_async` is exposed via core.db._prepare_engine_kwargs (returns URL + connect_args)

Both strip the `-pooler` suffix from the host (PgBouncer transaction-mode
breaks asyncpg prepared statements and alembic transactional DDL).
"""
from urllib.parse import urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


def _strip_pooler_from_host(host: str | None) -> str | None:
    """Neon -pooler endpoint → direct endpoint."""
    if not host:
        return host
    # Hostname pattern: ep-<slug>-pooler.<region>.<provider>.neon.tech
    # Strip the literal substring `-pooler` from the first label only.
    parts = host.split(".", 1)
    if len(parts) == 2:
        first, rest = parts
        if first.endswith("-pooler"):
            return first[: -len("-pooler")] + "." + rest
    return host


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str
    bitfinex_api_base_url: str = "https://api-pub.bitfinex.com"
    log_level: str = "INFO"

    @property
    def database_url_sync(self) -> str:
        """Sync URL for alembic (psycopg driver).

        Transforms:
        - scheme: postgresql:// or postgres:// → postgresql+psycopg://
        - host: strip `-pooler` suffix from first label
        - query params: sslmode + channel_binding preserved (psycopg supports both)
        """
        raw = self.database_url
        # Scheme normalisation
        if raw.startswith("postgresql://"):
            raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
        elif raw.startswith("postgres://"):
            raw = "postgresql+psycopg://" + raw[len("postgres://"):]
        elif raw.startswith("postgresql+"):
            head, _, tail = raw.partition("://")
            raw = "postgresql+psycopg://" + tail

        # urlsplit then rebuild with cleaned host
        parts = urlsplit(raw)
        new_host = _strip_pooler_from_host(parts.hostname)
        # Rebuild netloc preserving userinfo + port
        userinfo = ""
        if parts.username is not None:
            userinfo = parts.username
            if parts.password is not None:
                userinfo += f":{parts.password}"
            userinfo += "@"
        port_suffix = f":{parts.port}" if parts.port is not None else ""
        netloc = f"{userinfo}{new_host or ''}{port_suffix}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
