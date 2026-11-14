"""The mutation sandbox copies what the tests need, and only that, and runs pytest in the copy."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import mutation_sandbox as sandbox


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / "backend" / "src").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("ignored/\nmutants/\n")
    (root / "backend" / "src" / "tracked.py").write_text("A = 1\n")
    (root / "linked.py").symlink_to("backend/src/tracked.py")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    (root / "backend" / "src" / "untracked.py").write_text("B = 2\n")  # work not yet added
    (root / "ignored").mkdir()
    (root / "ignored" / "secret.txt").write_text("no\n")
    (root / "mutants").mkdir()
    (root / "mutants" / "stale.py").write_text("old mutant\n")
    monkeypatch.setattr(sandbox, "REPO", root)
    return root


def test_the_copy_holds_tracked_and_untracked_files_but_nothing_ignored(
    repo: Path, tmp_path: Path,
) -> None:
    copy = tmp_path / "copy"
    sandbox.copy_repository(copy)
    assert (copy / "backend/src/tracked.py").read_text() == "A = 1\n"
    assert (copy / "backend/src/untracked.py").read_text() == "B = 2\n"
    assert not (copy / "ignored").exists()
    assert not (copy / "mutants").exists()
    assert (copy / "linked.py").is_symlink()
    assert (copy / "linked.py").readlink() == Path("backend/src/tracked.py")


def test_copying_never_writes_to_the_working_tree(repo: Path, tmp_path: Path) -> None:
    before = sandbox.digest([Path("backend/src/tracked.py"), Path("backend/src/untracked.py")])
    copy = tmp_path / "copy"
    sandbox.copy_repository(copy)
    (copy / "backend/src/tracked.py").write_text("A = 'mutant'\n")
    after = sandbox.digest([Path("backend/src/tracked.py"), Path("backend/src/untracked.py")])
    assert after == before


def test_the_digest_follows_content_not_listing_order(repo: Path) -> None:
    paths = [Path("backend/src/tracked.py"), Path("backend/src/untracked.py")]
    before = sandbox.digest(paths)
    assert sandbox.digest(list(reversed(paths))) == before
    (repo / "backend/src/untracked.py").write_text("B = 3\n")
    assert sandbox.digest(paths) != before


def test_pytest_runs_from_the_copys_backend(tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    (copy / "backend").mkdir(parents=True)
    (copy / "backend" / "test_where.py").write_text(
        "import os\n\ndef test_where():\n    assert os.getcwd().endswith('copy/backend')\n"
        "    assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'\n")
    result = sandbox.pytest(copy, ["-q", "test_where.py"], timeout=120)
    assert result is not None and result.returncode == 0, result and result.stdout
    assert not list(copy.rglob("*.pyc"))


def test_pytest_keeps_bytecode_only_when_asked(tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    (copy / "backend").mkdir(parents=True)
    (copy / "backend" / "helper.py").write_text("VALUE = 1\n")
    (copy / "backend" / "test_cached.py").write_text(
        "import os\n\nimport helper\n\n\ndef test_cached():\n"
        "    assert helper.VALUE == 1\n    assert not os.environ['PYTHONDONTWRITEBYTECODE']\n")
    # The caller's environment may send bytecode elsewhere; the cache belongs in the copy.
    result = sandbox.pytest(copy, ["-q", "test_cached.py"], timeout=120, bytecode=True,
                            env={"PYTHONPYCACHEPREFIX": ""})
    assert result is not None and result.returncode == 0, result and result.stdout
    assert [path.name.split(".")[0] for path in copy.rglob("helper*.pyc")] == ["helper"]
