"""One throwaway PostgreSQL 18 per pytest process, started from local binaries (no Docker).

The server comes from ``pytest-postgresql``'s process fixture, launched from the PostgreSQL 18
binaries found by :func:`find_pg_bin`. Production runs ``postgres:18-alpine`` (see
``deploy/vm/postgres/Dockerfile``), so the suite refuses any other major version: the major is
parsed out of that Dockerfile's ``FROM`` line, which stays the single source.

initdb uses the ``builtin`` C.UTF-8 collation provider. Production is libc ``en_US.utf8`` on
musl, which orders by code point; ``C.UTF-8`` reproduces that on macOS (which has no
``C.UTF-8`` libc locale) and on glibc alike.

Each pytest process (so each xdist worker) owns one server on a random loopback port, with its
own data directory and a unix socket in a short directory (macOS caps socket paths at 103
bytes), all removed when the session ends.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_DOCKERFILE = ROOT / "deploy" / "vm" / "postgres" / "Dockerfile"
BIN_ENV = "BFX_TEST_PG_BIN"
HOMEBREW_PG18_BIN = Path("/opt/homebrew/opt/postgresql@18/bin")
INITDB_LOCALE_OPTIONS = (
    "--encoding=UTF8",
    "--locale=C",
    "--locale-provider=builtin",
    "--builtin-locale=C.UTF-8",
)
# fsync/full_page_writes/synchronous_commit: a test-only durability trade, every database here is
# disposable. timezone: initdb would take the host's zone, but the production container is UTC and
# tests compare timestamps rendered by the server.
SERVER_OPTIONS = "-c fsync=off -c full_page_writes=off -c synchronous_commit=off -c timezone=UTC"
INSTALL_HINT = (
    "Install PostgreSQL {major} and point {env} at its bin directory: "
    "`brew install postgresql@{major}` (macOS; found automatically at {homebrew}) or the PGDG apt "
    "repository (`apt install postgresql-{major}`, bin dir /usr/lib/postgresql/{major}/bin). "
    "The tests never fall back to Docker or to another PostgreSQL major."
)


class PgBinNotFoundError(RuntimeError):
    pass


class PgVersionMismatchError(RuntimeError):
    pass


def postgres_image_reference(dockerfile: str | None = None) -> str:
    """``postgres:18-alpine@sha256:...``: the first ``FROM postgres:...`` of the VM image."""
    text = POSTGRES_DOCKERFILE.read_text() if dockerfile is None else dockerfile
    match = re.search(r"^FROM\s+(postgres:\S+@sha256:[0-9a-f]{64})\b", text, re.MULTILINE)
    if match is None:
        raise ValueError("no `FROM postgres:<tag>@sha256:<digest>` line in the Dockerfile")
    return match.group(1)


def dockerfile_pg_major(dockerfile: str | None = None) -> int:
    match = re.match(r"postgres:(\d+)", postgres_image_reference(dockerfile))
    if match is None:
        raise ValueError("the Dockerfile's postgres tag does not start with a major version")
    return int(match.group(1))


def find_pg_bin(
    environ: Mapping[str, str] | None = None, *, homebrew: Path = HOMEBREW_PG18_BIN,
    pg_config: str | None = None, major: int | None = None,
) -> Path | None:
    """The directory with ``pg_ctl``/``initdb``/``pg_dump``, or None when none is installed.

    ``BFX_TEST_PG_BIN`` wins and must be valid (a typo is an error, never a silent fallback).
    Otherwise the Homebrew ``postgresql@18`` keg, then the PGDG apt layout, then
    ``pg_config --bindir``. An unversioned ``postgresql`` formula is never looked up by name.
    """
    environ = os.environ if environ is None else environ
    major = dockerfile_pg_major() if major is None else major
    explicit = environ.get(BIN_ENV)
    if explicit:
        directory = Path(explicit)
        if not (directory / "pg_ctl").is_file():
            raise PgBinNotFoundError(f"{BIN_ENV}={explicit} has no pg_ctl. " + _hint(major))
        return directory
    candidates = [homebrew, Path(f"/usr/lib/postgresql/{major}/bin")]
    for candidate in candidates:
        if (candidate / "pg_ctl").is_file():
            return candidate
    tool = pg_config or shutil.which("pg_config")
    if tool:
        try:
            completed = subprocess.run([tool, "--bindir"], capture_output=True, text=True,
                                       check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        directory = Path(completed.stdout.strip())
        if completed.returncode == 0 and (directory / "pg_ctl").is_file():
            return directory
    return None


def _hint(major: int) -> str:
    return INSTALL_HINT.format(major=major, env=BIN_ENV, homebrew=HOMEBREW_PG18_BIN)


def missing_bin_message(major: int | None = None) -> str:
    major = dockerfile_pg_major() if major is None else major
    return f"No PostgreSQL {major} binaries found. " + _hint(major)


def tool_major(bin_dir: Path, tool: str = "pg_ctl") -> int:
    """Major version a binary reports (``pg_ctl (PostgreSQL) 18.6`` -> 18)."""
    out = subprocess.run([str(bin_dir / tool), "--version"], capture_output=True, text=True,
                         check=True, timeout=30).stdout
    match = re.search(r"\)\s+(\d+)(?:\.\d+)?", out)
    if match is None:
        raise PgVersionMismatchError(f"cannot read the version from `{tool} --version`: {out!r}")
    return int(match.group(1))


def check_major(found: int, expected: int, what: str) -> None:
    if found != expected:
        raise PgVersionMismatchError(
            f"{what} is PostgreSQL {found}, but deploy/vm/postgres/Dockerfile pins "
            f"postgres:{expected}. The suite runs on the production major only. "
            + _hint(expected)
        )


@dataclass(frozen=True)
class LocalPostgres:
    """The per-process server, shaped like the testcontainers object tests used to receive."""

    bin_dir: Path
    host: str
    port: int
    username: str
    password: str
    dbname: str
    data_directory: Path
    socket_directory: Path
    temp_root: Path  # the fixture's own directory; the data directory lives under it

    def get_connection_url(self) -> str:
        return (f"postgresql+psycopg2://{self.username}:{self.password}"
                f"@{self.host}:{self.port}/{self.dbname}")

    def tool(self, name: str) -> str:
        return str(self.bin_dir / name)

    def tool_env(self) -> dict[str, str]:
        return {**os.environ, "PGPASSWORD": self.password, "PGHOST": self.host,
                "PGPORT": str(self.port), "PGUSER": self.username}


def short_socket_directory() -> str:
    """A directory whose socket path fits the 103-byte limit (macOS temp dirs do not)."""
    return tempfile.mkdtemp(prefix="bfxpg", dir="/tmp")


def install_executor() -> None:
    """Make pytest-postgresql's process factory initdb with the builtin collation provider.

    The factory offers no initdb options and instantiates ``PostgreSQLExecutor`` by name, so the
    subclass below replaces that name in the factory module. It also puts the unix socket in a
    short directory of its own and removes it when the server stops.
    """
    import pytest_postgresql.factories.process as process
    from pytest_postgresql.executors import PostgreSQLExecutor

    class BuiltinLocaleExecutor(PostgreSQLExecutor):
        def __init__(self, *args: object, unixsocketdir: str, **kwargs: object) -> None:
            self.socket_directory = short_socket_directory()
            super().__init__(*args, unixsocketdir=self.socket_directory, **kwargs)  # type: ignore[arg-type]

        def _format_initdb_options(self, initdb_options: list[str]) -> str:
            return super()._format_initdb_options([*initdb_options, *INITDB_LOCALE_OPTIONS])

        def stop(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            try:
                return super().stop(*args, **kwargs)  # type: ignore[arg-type]
            finally:
                shutil.rmtree(self.socket_directory, ignore_errors=True)

    process.PostgreSQLExecutor = BuiltinLocaleExecutor  # type: ignore[misc]
