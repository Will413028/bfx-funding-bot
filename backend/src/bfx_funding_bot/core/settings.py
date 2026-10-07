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


class AuthSettings(BaseSettings):
    """Runtime configuration needed by the auth boundary without a database.

    Keeping this model separate from :class:`Settings` lets the web process
    import its liveness endpoint even when the database URL is absent or
    malformed.  Database configuration remains required by ``Settings`` and
    is checked by the startup/readiness path instead of at module import time.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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
    def _validate_production_operator_role(self) -> "AuthSettings":
        """Production is deliberately locked to the sole admin operator role."""
        phase = os.environ.get("BFX_PHASE", "").lower()
        if phase == "live" and self.operator_role != "admin":
            raise ValueError("BFX_OPERATOR_ROLE must be 'admin' in live")
        return self


class Settings(AuthSettings):
    """Full application settings, including the required database URL."""

    database_url: str = Field(repr=False)
    bitfinex_api_base_url: str = "https://api-pub.bitfinex.com"
    log_level: str = "INFO"

    # SP2 vault: the api-key envelope KEK is the env var BFX_VAULT_KEK
    # (base64-encoded 32 bytes), read DIRECTLY from os.environ by
    # core.crypto.load_kek() — intentionally NOT a Settings field, so crypto
    # unit tests don't require DATABASE_URL (which Settings() needs via the
    # .env symlink). Deploy presence is enforced by deploy-vm.sh preflight.

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
