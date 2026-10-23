"""Collect the SQL a PR's new Alembic revisions actually run, for squawk.

Offline ``alembic upgrade --sql`` cannot render these migrations: they read data and
assert on it (ADR 2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them).
So the revisions the PR adds run online against a throwaway PostgreSQL: the database is
upgraded to just before them unlogged, then ``log_statement=all`` is switched on and it
is upgraded to head. Every statement the server received in that second run -- env.py's
``SET lock_timeout``, the transaction, the DDL and the data checks -- goes to ``--out``
in order, so squawk lints what production will execute.

Usage (CI; PG18 binaries on BFX_TEST_PG_BIN or PATH):
    cd backend
    uv run python scripts/migration_ddl.py --base-ref origin/main --out migration.sql

Exit codes: 0 = done (``--out`` is empty when the PR adds no revision); 1 = a revision
failed to apply, or the new revisions do not hang off one existing revision.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
VERSIONS = "backend/alembic/versions"
# Simple-protocol statements log as "statement: ..."; psycopg's parameterised ones as
# "execute <name>: ..." (the values are in the record's detail, not needed here).
STATEMENT = re.compile(r"^(?:statement|execute [^:]+): ", re.DOTALL)
# The switch that turns logging off again is logged too; it is not the migration's.
OWN_STATEMENTS = ("ALTER SYSTEM RESET log_statement", "SELECT pg_reload_conf()")


def added_revision_files(base_ref: str) -> list[str]:
    """Revision files the PR adds (an unknown ref fails here, never reads as none)."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", "--diff-filter=A", f"{base_ref}...HEAD",
         "--", f"{VERSIONS}/*.py"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def new_revisions(script: ScriptDirectory, files: Iterable[str]) -> set[str]:
    names = {Path(f).name for f in files}
    return {
        rev.revision for rev in script.walk_revisions()
        if rev.path is not None and Path(rev.path).name in names
    }


def upgrade_base(script: ScriptDirectory, new: set[str]) -> str:
    """The one existing revision the new ones hang off."""
    parents: set[str] = set()
    for revision in new:
        down = script.get_revision(revision).down_revision
        for parent in (down,) if isinstance(down, str) or down is None else down:
            if parent is not None and parent not in new:
                parents.add(parent)
    if len(parents) != 1:
        raise SystemExit(f"new revisions {sorted(new)} must branch from one revision, got {sorted(parents)}")
    return parents.pop()


def logged_statements(log_dir: Path) -> list[str]:
    """``statement:`` messages from the server's jsonlog, in the order it logged them."""
    statements: list[str] = []
    for log in sorted(log_dir.glob("*.json")):
        for line in log.read_text().splitlines():
            message = json.loads(line).get("message", "")
            if (match := STATEMENT.match(message)) is not None:
                statement = message[match.end():].strip().rstrip(";")
                if statement not in OWN_STATEMENTS:
                    statements.append(statement)
    return statements


def render(statements: Iterable[str]) -> str:
    return "".join(f"{s};\n" for s in statements)


def _pg_tool(name: str) -> str:
    bin_dir = os.environ.get("BFX_TEST_PG_BIN")
    tool = str(Path(bin_dir) / name) if bin_dir else shutil.which(name)
    if tool is None or not Path(tool).is_file():
        raise SystemExit(f"{name} not found: set BFX_TEST_PG_BIN to the PostgreSQL 18 bin directory")
    return tool


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def throwaway_postgres(root: Path) -> Iterator[tuple[str, Path]]:
    """A local server on 127.0.0.1; yields (DATABASE_URL, jsonlog directory).

    TCP, not a unix socket: alembic.ini interpolates ``%``, which a percent-encoded
    socket path in the URL is full of."""
    data, logs = root / "data", root / "log"
    logs.mkdir()
    port = _free_port()
    subprocess.run([_pg_tool("initdb"), "-D", str(data), "-U", "postgres", "--auth=trust",
                    "--encoding=UTF8", "--locale-provider=builtin", "--builtin-locale=C.UTF-8"],
                   check=True, capture_output=True)
    options = (f"-c listen_addresses=127.0.0.1 -p {port} -k '' -c fsync=off -c timezone=UTC "
               f"-c logging_collector=on -c log_destination=jsonlog -c log_directory={logs}")
    # -l: the server's own output goes to a file. Captured instead, the server would hold
    # the pipe open and the start would never return.
    subprocess.run([_pg_tool("pg_ctl"), "-D", str(data), "-o", options, "-l", str(root / "server.log"),
                    "-w", "start"], check=True, capture_output=True)
    try:
        url = f"postgresql://postgres@127.0.0.1:{port}/ci"
        subprocess.run([_pg_tool("createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "ci"],
                       check=True, capture_output=True)
        yield url, logs
    finally:
        subprocess.run([_pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                       check=False, capture_output=True)


def _psql(url: str, *statements: str) -> None:
    # One -c per statement: ALTER SYSTEM refuses to run inside the transaction psql wraps
    # around a multi-statement -c.
    args = [arg for statement in statements for arg in ("-c", statement)]
    subprocess.run([_pg_tool("psql"), url, "-v", "ON_ERROR_STOP=1", "-q", *args],
                   check=True, capture_output=True)


def _alembic(url: str, revision: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision], cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout + completed.stderr)
        raise SystemExit(f"alembic upgrade {revision} failed")


def collect(base: str) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="bfx-mig-") as tmp, \
            throwaway_postgres(Path(tmp)) as (url, logs):
        _alembic(url, base)
        _psql(url, "ALTER SYSTEM SET log_statement = 'all'", "SELECT pg_reload_conf()")
        _alembic(url, "head")
        _psql(url, *OWN_STATEMENTS)
        return logged_statements(logs)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-ref", required=True, help="the commit the PR merges into")
    parser.add_argument("--out", type=Path, required=True, help="SQL file for squawk")
    args = parser.parse_args(argv)

    files = added_revision_files(args.base_ref)
    if not files:
        args.out.write_text("")
        print("no new Alembic revision")
        return 0
    script = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))
    new = new_revisions(script, files)
    base = upgrade_base(script, new)
    print(f"new revisions: {', '.join(sorted(new))}; upgrading from {base}")
    args.out.write_text(render(collect(base)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
