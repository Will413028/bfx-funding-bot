"""The PR size gate counts reviewable lines and only lets a justified large PR through."""
import subprocess
from pathlib import Path

import pytest

from scripts import pr_size

LIMIT = pr_size.LIMIT


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _write(repo: Path, path: str, lines: int, word: str = "line") -> None:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("".join(f"{word} {i}\n" for i in range(lines)))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    _git(tmp_path, "config", "user.name", "ci")
    _write(tmp_path, "README.md", 1)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "tag", "base")
    return tmp_path


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")


def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, body: str = "") -> int:
    monkeypatch.setattr(pr_size, "REPO", repo)
    monkeypatch.setenv("PR_BODY", body)
    return pr_size.main(["--base-ref", "base"])


def test_one_line_over_the_limit_needs_a_justification(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, "src/app.py", LIMIT + 1)
    _commit(repo)
    assert _run(repo, monkeypatch) == 1
    assert _run(repo, monkeypatch, body="Summary\n\nSize-Justification: one atomic rename\n") == 0


def test_exactly_the_limit_passes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo, "src/app.py", LIMIT)
    _commit(repo)
    assert _run(repo, monkeypatch) == 0


def test_deleted_lines_count_too(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo, "src/app.py", 600)
    _commit(repo)
    _git(repo, "tag", "-f", "base")
    _write(repo, "src/app.py", 600, word="other")
    _commit(repo)
    assert _run(repo, monkeypatch) == 1


@pytest.mark.parametrize(
    "path",
    [
        "backend/uv.lock",
        "deploy/vm/ops/uv.lock",
        "frontend/pnpm-lock.yaml",
        "infra/terraform/r2/.terraform.lock.hcl",
        "backend/alembic/versions/abc123_add_table.py",
    ],
)
def test_lockfiles_and_migrations_are_not_counted(
    repo: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _write(repo, path, 2 * LIMIT)
    _commit(repo)
    assert _run(repo, monkeypatch) == 0


def test_a_lockfile_name_elsewhere_in_a_path_is_still_counted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, "src/uv.lock.py", LIMIT + 1)
    _write(repo, "backend/alembic/env_versions.py", 1)
    _commit(repo)
    assert _run(repo, monkeypatch) == 1


def test_linguist_generated_paths_are_not_counted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".gitattributes").write_text("src/gen/** linguist-generated\n")
    _write(repo, "src/gen/client.ts", 2 * LIMIT)
    _commit(repo)
    assert _run(repo, monkeypatch) == 0


def test_a_generated_attribute_set_false_is_counted(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".gitattributes").write_text("src/gen/** -linguist-generated\n")
    _write(repo, "src/gen/client.ts", LIMIT + 1)
    _commit(repo)
    assert _run(repo, monkeypatch) == 1


def test_a_pure_rename_counts_no_lines(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(repo, "src/old_name.py", 2 * LIMIT)
    _commit(repo)
    _git(repo, "tag", "-f", "base")
    _git(repo, "mv", "src/old_name.py", "src/new_name.py")
    _commit(repo)
    assert _run(repo, monkeypatch) == 0


def test_a_rename_into_a_lockfile_path_is_judged_by_its_new_path() -> None:
    raw = "0\t0\t\0src/a.txt\0backend/uv.lock\0" "5\t0\tsrc/b.py\0"
    changes = pr_size.parse_numstat(raw)
    assert changes == [pr_size.FileChange("backend/uv.lock", 0), pr_size.FileChange("src/b.py", 5)]


def test_binary_files_count_no_lines() -> None:
    changes = pr_size.parse_numstat("-\t-\tdocs/diagram.png\0" "3\t2\tsrc/a.py\0")
    assert pr_size.counted_lines(changes, frozenset()) == 5


@pytest.mark.parametrize(
    "body",
    [
        "",
        "Size-Justification:",
        "Size-Justification:   \n",
        "size-justification: lowercase key",
        "  Size-Justification: indented, so not a trailer line",
        "No Size-Justification: mid-line",
    ],
)
def test_a_missing_or_empty_justification_does_not_count(body: str) -> None:
    assert pr_size.check(LIMIT + 1, body)


def test_a_justification_line_anywhere_in_the_body_counts() -> None:
    body = "## Summary\nbig move\n\nSize-Justification: generated fixtures moved as one unit\n"
    assert pr_size.check(LIMIT + 1, body) == []


def test_an_unknown_base_ref_fails_instead_of_counting_nothing(repo: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        pr_size.diff_changes(repo, "no-such-ref")
