"""Application settings.

DATABASE_URL is a libpq URL (``postgresql://u:p@host/db``). Two accessors transform it for the
two SQLAlchemy drivers we use:

- `database_url_sync` — psycopg, for alembic env.py
- `database_url_async` is exposed via core.db._prepare_engine_kwargs (returns URL + connect_args)

Both read the TLS mode through `database_sslmode`, so the two drivers agree on it.
"""
import os
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bfx_funding_bot.core.secret_files import read_secret

VALID_DEPLOYMENT_ENVIRONMENTS = frozenset({"prod", "shadow", "ci"})


def require_deployment_environment() -> str:
    """Return an explicit, known data-isolation environment.

    A missing value must never silently select ``prod``.  This helper is used
    by web/API and operator scripts, which do not pass through marketfeed's
    stricter ``load_config`` startup validator.
    """
    value = os.environ.get("BFX_DEPLOYMENT_ENV", "").strip()
    if value not in VALID_DEPLOYMENT_ENVIRONMENTS:
        valid = ", ".join(sorted(VALID_DEPLOYMENT_ENVIRONMENTS))
        raise ValueError(
            f"BFX_DEPLOYMENT_ENV must be explicitly set to one of {valid}"
        )
    return value


# libpq's modes that verify the server, plus an explicit plaintext one. ``require``, ``prefer``
# and ``allow`` encrypt without knowing who answers, so they stop no man in the middle.
DATABASE_SSLMODES = frozenset({"disable", "verify-ca", "verify-full"})


def database_sslmode(query: dict[str, list[str]]) -> str:
    """The TLS mode of a DATABASE_URL query: an accepted libpq ``sslmode``, else a ValueError.

    No ``sslmode`` is ``disable`` (both drivers would otherwise try TLS without verifying it).
    asyncpg's own ``ssl=`` parameter is refused: it bypasses this check.
    """
    if "ssl" in query:
        raise ValueError("DATABASE_URL: use libpq sslmode=, not ssl=")
    mode = query.get("sslmode", ["disable"])[0]
    if mode not in DATABASE_SSLMODES:
        raise ValueError(
            f"DATABASE_URL: sslmode={mode} does not verify the server; use verify-full "
            "(sslrootcert= for a private CA), or disable on a private network"
        )
    return mode


class IngressSettings(BaseSettings):
    """The web API's tailnet ingress (9d-5): every value required at every web API start.

    Built once in the web API's lifespan and kept on ``app.state.ingress``; never at module
    import (the import must work without these keys) and never on the ``Settings`` chain
    (other processes build ``Settings`` without them). Not gated on ``BFX_PHASE``: the web
    API reads only ``webapi.env``, which carries no phase, and has no mode without Serve.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # The operator's tailnet node user ID: what the Serve-forwarded operator capability must
    # name, and the principal's key (the membership user_id).
    operator_tailnet_user_id: str = Field(validation_alias="BFX_OPERATOR_TAILNET_USER_ID", min_length=1)
    # Comma-separated Host values the web API answers (the node's MagicDNS name).
    allowed_hosts_csv: str = Field(validation_alias="BFX_WEBAPI_ALLOWED_HOSTS", min_length=1)
    # /fe-events forwards only to this DSN's Sentry project.
    fe_sentry_dsn: str = Field(validation_alias="BFX_FE_SENTRY_DSN", min_length=1, repr=False)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(host.strip().lower() for host in self.allowed_hosts_csv.split(",") if host.strip())

    @model_validator(mode="after")
    def _validate(self) -> "IngressSettings":
        if not self.operator_tailnet_user_id.strip():
            raise ValueError("BFX_OPERATOR_TAILNET_USER_ID must not be blank")
        hosts = self.allowed_hosts
        if not hosts or any("/" in host or ":" in host or "*" in host for host in hosts):
            raise ValueError("BFX_WEBAPI_ALLOWED_HOSTS must list bare host names")
        dsn = urlsplit(self.fe_sentry_dsn)
        if dsn.scheme != "https" or not dsn.username or not dsn.hostname or not dsn.path.strip("/"):
            raise ValueError("BFX_FE_SENTRY_DSN must be an https Sentry DSN")
        try:
            _ = dsn.port  # raises on a non-numeric or out-of-range port
        except ValueError as exc:
            raise ValueError("BFX_FE_SENTRY_DSN has an invalid port") from exc
        return self


class Settings(BaseSettings):
    """Full application settings, including the required database URL."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(repr=False)
    bitfinex_api_base_url: str = "https://api-pub.bitfinex.com"
    log_level: str = "INFO"

    # SP2 vault: the api-key envelope KEK (BFX_VAULT_KEK, base64-encoded 32 bytes) is
    # read by core.crypto.load_kek() through core.secret_files -- intentionally NOT a
    # Settings field, so crypto unit tests don't require DATABASE_URL.

    @property
    def database_url_sync(self) -> str:
        """Sync URL for alembic (psycopg driver).

        - scheme: postgresql:// or postgres:// or postgresql+<any> → postgresql+psycopg://
        - sslmode: checked by `database_sslmode` and always written out (none → disable)
        """
        raw = self.database_url
        if raw.startswith("postgresql://"):
            raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
        elif raw.startswith("postgres://"):
            raw = "postgresql+psycopg://" + raw[len("postgres://"):]
        elif raw.startswith("postgresql+"):
            _head, _, tail = raw.partition("://")
            raw = "postgresql+psycopg://" + tail

        parts = urlsplit(raw)
        query = parse_qs(parts.query, keep_blank_values=True)
        query["sslmode"] = [database_sslmode(query)]
        new_query = urlencode({k: v[0] for k, v in query.items()})
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def load_settings() -> Settings:
    """``Settings`` for a resident service: DATABASE_URL through core.secret_files.

    With ``BFX_SECRETS_DIR`` (the web API in production) the URL comes only from its
    secret file (missing or empty raises ``SecretFileError``). Without it, the env value
    when set, else pydantic's own sources (the ``.env`` file of a local run)."""
    database_url = read_secret("DATABASE_URL")
    if database_url is None:
        return Settings()
    return Settings(database_url=database_url)
