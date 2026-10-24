"""The one reader of the resident services' secrets: files when ``BFX_SECRETS_DIR`` is set.

The bot and the web API run with ``BFX_SECRETS_DIR=/run/secrets``: Compose mounts each
secret as a file there (``deploy/vm/docker-compose.app.yml``, shape enforced by
``deploy/vm/ops/compose_policy.py``, host files checked by ``bfx_deploy.py``). In that mode
a key of :data:`FILE_SECRET_KEYS` is read only from ``<dir>/<KEY>``: a missing or empty file
is a :class:`SecretFileError`, and the environment's value of the key is ignored -- never a
fallback, so a secret cannot silently come from the wrong place (ADR 2026-08-31, Amendment 2026-10-22).

Every other process has no ``BFX_SECRETS_DIR`` and reads the same keys from its
environment: the one-shot containers (operator CLI, weekly report, research jobs, the
simulation soak, migrations), the DR checks, tests and local runs. That split is by
process, not a transition: one-shot containers live seconds to an hour and stay on env
files by decision.

Values are never written back to ``os.environ`` (that would put them in the environment
again) and never appear in an error message.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

SECRETS_DIR_ENV: Final = "BFX_SECRETS_DIR"
# Every secret a resident service may take from a file. The per-service sets are pinned in
# compose_policy.SERVICE_SECRETS (tests/core/test_secret_files.py keeps the two equal).
FILE_SECRET_KEYS: Final = frozenset({
    "DATABASE_URL", "BFX_ADMIN_TOKEN", "BFX_ADMIN_READ_TOKEN", "BFX_VAULT_KEK",
    "TELEGRAM_BOT_TOKEN",
})


class SecretFileError(ValueError):
    """A secret this process must read from a file is missing, empty or unreadable.

    A ValueError, so the bot's boot treats it as the config error it is (exit 1)."""


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def secrets_dir(environ: Mapping[str, str] | None = None) -> Path | None:
    """The secret file directory, or None when this process reads its secrets from env."""
    value = _environ(environ).get(SECRETS_DIR_ENV, "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise SecretFileError(f"{SECRETS_DIR_ENV} must be an absolute path")
    return path


def _known(name: str) -> None:
    if name not in FILE_SECRET_KEYS:
        raise KeyError(f"{name} is not a file secret")


def read_secret(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The secret ``name``, surrounding whitespace removed.

    With ``BFX_SECRETS_DIR``: the file's content; missing, empty or unreadable raises
    :class:`SecretFileError` (the env value is ignored). Without it: the env value, or None
    when unset or blank (each caller decides whether that is an error, as before).
    """
    _known(name)
    env = _environ(environ)
    directory = secrets_dir(env)
    if directory is None:
        return env.get(name, "").strip() or None
    path = directory / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise SecretFileError(f"secret file {path} is missing") from None
    except (OSError, UnicodeError) as exc:
        raise SecretFileError(f"secret file {path} is unreadable ({type(exc).__name__})") from None
    if not value:
        raise SecretFileError(f"secret file {path} is empty")
    return value


def secret_present(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process was handed ``name`` at all, either way: the key in its
    environment (even empty) or, with ``BFX_SECRETS_DIR``, a file of that name (even empty).

    For the refusals that a secret must NOT be there (the web API and the vault key, a
    simulated boot and the vault key)."""
    _known(name)
    env = _environ(environ)
    return name in env or secret_file_mounted(name, env)


def secret_file_mounted(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether, with ``BFX_SECRETS_DIR``, a file (or a dangling link) named ``name`` is there."""
    _known(name)
    directory = secrets_dir(environ)
    return directory is not None and os.path.lexists(directory / name)


def file_secret_values(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The non-empty secret files this process has, by key (empty without ``BFX_SECRETS_DIR``).

    For scrubbing alerts and error reports: never raises, so a broken mount cannot stop
    the scrubber from covering the files that do read."""
    try:
        directory = secrets_dir(environ)
    except SecretFileError:
        return {}
    if directory is None:
        return {}
    values = {}
    for name in sorted(FILE_SECRET_KEYS):
        try:
            value = (directory / name).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value:
            values[name] = value
    return values


def ignored_env_keys(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """With ``BFX_SECRETS_DIR``: the file secrets still set in the environment, which this
    process ignores (left in an env file after the cutover; remove them). Names only."""
    env = _environ(environ)
    try:
        directory = secrets_dir(env)
    except SecretFileError:
        return ()
    if directory is None:
        return ()
    return tuple(sorted(name for name in FILE_SECRET_KEYS if name in env))


def warn_ignored_env_keys(log: logging.Logger, environ: Mapping[str, str] | None = None) -> None:
    """Log the :func:`ignored_env_keys` once at boot (names only), so the env file left over
    from the cutover gets cleaned (docs/runbooks/deploy.md)."""
    ignored = ignored_env_keys(environ)
    if ignored:
        log.warning("secret_env_keys_ignored keys=%s: read from the secret files instead; "
                    "remove them from the env file", ",".join(ignored))


__all__ = [
    "FILE_SECRET_KEYS",
    "SECRETS_DIR_ENV",
    "SecretFileError",
    "file_secret_values",
    "ignored_env_keys",
    "read_secret",
    "secret_file_mounted",
    "secret_present",
    "secrets_dir",
    "warn_ignored_env_keys",
]
