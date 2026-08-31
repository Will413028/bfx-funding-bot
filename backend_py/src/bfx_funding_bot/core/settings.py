"""Application settings.

DATABASE_URL accepts EITHER form:
- Raw Neon dashboard libpq URL: `postgresql://u:p@ep-xxx-pooler.<region>.aws.neon.tech/db?sslmode=require&channel_binding=require`
- Phase 4.1 manually-transformed asyncpg URL: `postgresql+asyncpg://u:p@ep-xxx.<region>.aws.neon.tech/db?ssl=require`

Two accessors transform it for the two SQLAlchemy drivers we use:

- `database_url_sync` — psycopg, for alembic env.py
- `database_url_async` is exposed via core.db._prepare_engine_kwargs (returns URL + connect_args)

Both strip the `-pooler` suffix from the host (PgBouncer transaction-mode
breaks asyncpg prepared statements and alembic transactional DDL).
"""
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, model_validator
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

    # SP1 web-API auth (Better Auth JWKS verification)
    better_auth_jwks_url: str = ""        # deploy: https://<app>/api/auth/jwks (real endpoint)
    # Pinned stable issuer — must byte-match the FE jwt plugin's `issuer` (frontend
    # src/lib/auth.ts), NOT the deploy URL, so a domain change can't break verify.
    # Override via env BETTER_AUTH_ISSUER only if the FE issuer string ever changes.
    better_auth_issuer: str = "bfx-funding-bot"
    jwt_audience: str = "bfx-funding-backend"
    operator_user_id: str = Field(default="", validation_alias="BFX_OPERATOR_USER_ID")
    operator_role: str = Field(default="admin", validation_alias="BFX_OPERATOR_ROLE")

    @model_validator(mode="after")
    def _validate_production_operator_role(self) -> "Settings":
        """Production is deliberately locked to the sole admin operator role."""
        phase = os.environ.get("BFX_PHASE", "").lower()
        if phase in {"canary", "live"} and self.operator_role != "admin":
            raise ValueError("BFX_OPERATOR_ROLE must be 'admin' in canary or live")
        return self

    # SP2 vault: the api-key envelope KEK is the env var BFX_VAULT_KEK
    # (base64-encoded 32 bytes), read DIRECTLY from os.environ by
    # core.crypto.load_kek() — intentionally NOT a Settings field, so crypto
    # unit tests don't require DATABASE_URL (which Settings() needs via the
    # .env symlink). Deploy presence is enforced by deploy-vm.sh preflight.

    @property
    def database_url_sync(self) -> str:
        """Sync URL for alembic (psycopg driver).

        Transforms:
        - scheme: postgresql:// or postgres:// or postgresql+<any> → postgresql+psycopg://
        - host: strip `-pooler` suffix from first label
        - query params:
            - sslmode preserved (psycopg native)
            - ssl=<x> → sslmode=<x> (Phase 4.1 asyncpg-style → psycopg-style)
            - channel_binding preserved if present
        """
        raw = self.database_url
        # Scheme normalisation
        if raw.startswith("postgresql://"):
            raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
        elif raw.startswith("postgres://"):
            raw = "postgresql+psycopg://" + raw[len("postgres://"):]
        elif raw.startswith("postgresql+"):
            _head, _, tail = raw.partition("://")
            raw = "postgresql+psycopg://" + tail

        # urlsplit then rebuild with cleaned host + normalised query
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

        # Translate ssl=<x> → sslmode=<x> for psycopg compatibility
        # (Phase 4.1 manually-transformed URLs use asyncpg's `ssl=` keyword)
        qs_pairs = parse_qsl(parts.query, keep_blank_values=True)
        normalised: list[tuple[str, str]] = []
        for k, v in qs_pairs:
            if k == "ssl":
                normalised.append(("sslmode", v))
            else:
                normalised.append((k, v))
        new_query = urlencode(normalised)
        return urlunsplit((parts.scheme, netloc, parts.path, new_query, parts.fragment))
