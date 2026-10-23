"""The migration lint feeds squawk every statement the new revisions sent, nothing else."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import migration_ddl


def _log(path: Path, *messages: str) -> None:
    path.write_text("".join(json.dumps({"message": m, "error_severity": "LOG"}) + "\n"
                            for m in messages))


def test_logged_statements_keep_both_protocols_in_order_and_drop_the_switch(tmp_path: Path) -> None:
    _log(tmp_path / "postgresql-1.json",
         "database system is ready to accept connections",
         "statement: SET lock_timeout = '5s'",
         "execute <unnamed>: SELECT 1 FROM pg_roles WHERE rolname=$1",
         "statement: CREATE INDEX ix ON t (c);",
         "statement: ALTER SYSTEM RESET log_statement",
         "statement: SELECT pg_reload_conf()")
    assert migration_ddl.logged_statements(tmp_path) == [
        "SET lock_timeout = '5s'",
        "SELECT 1 FROM pg_roles WHERE rolname=$1",
        "CREATE INDEX ix ON t (c)",
    ]


def test_logged_statements_keep_a_multi_line_statement_whole(tmp_path: Path) -> None:
    _log(tmp_path / "postgresql-1.json", "statement: ALTER TABLE t\n    ADD COLUMN c int")
    assert migration_ddl.logged_statements(tmp_path) == ["ALTER TABLE t\n    ADD COLUMN c int"]


def test_render_ends_every_statement() -> None:
    assert migration_ddl.render(["BEGIN", "CREATE TABLE t (c int)"]) == "BEGIN;\nCREATE TABLE t (c int);\n"


class _Script:
    def __init__(self, graph: dict[str, str | tuple[str, ...] | None]) -> None:
        self._graph = graph

    def get_revision(self, revision: str) -> SimpleNamespace:
        return SimpleNamespace(down_revision=self._graph[revision])


def test_upgrade_base_is_the_revision_a_chain_of_new_ones_hangs_off() -> None:
    script = _Script({"b": "a", "c": "b"})
    assert migration_ddl.upgrade_base(script, {"b", "c"}) == "a"


def test_upgrade_base_refuses_new_revisions_on_two_parents() -> None:
    script = _Script({"x": "a", "y": "b"})
    with pytest.raises(SystemExit, match="must branch from one revision"):
        migration_ddl.upgrade_base(script, {"x", "y"})


def test_upgrade_base_follows_a_merge_revision_to_its_one_existing_parent() -> None:
    script = _Script({"m": ("a", "n"), "n": "a"})
    assert migration_ddl.upgrade_base(script, {"m", "n"}) == "a"
