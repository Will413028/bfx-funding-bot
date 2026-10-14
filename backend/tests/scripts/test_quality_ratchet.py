"""The quality ratchet counts every suppression spelling and only lets the lock tighten."""
import json
import subprocess
from pathlib import Path

import pytest

from scripts import quality_ratchet as ratchet

BACKEND = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("x = 1  # noqa: E501", "noqa"),
        ("x = 1  #noqa", "noqa"),
        ("# ruff: noqa: F401", "ruff: ignore"),
        ("x = 1  # ruff: ignore[E501]", "ruff: ignore"),
        ("x: int = y  # type: ignore[assignment]", "type: ignore"),
        ("x: int = y  # type:ignore", "type: ignore"),
        ("x: int = y  # pyright: ignore[reportGeneralTypeIssues]", "pyright: ignore"),
        ("// biome-ignore lint/style/noNonNullAssertion: checked above", "biome-ignore"),
        ("// biome-ignore-all lint/suspicious/noExplicitAny: generated", "biome-ignore"),
        ("// eslint-disable-next-line", "eslint-disable"),
        ("/* eslint-disable */", "eslint-disable"),
        ("// @ts-expect-error wrong upstream type", "ts-expect-error"),
        ("// @ts-ignore", "ts-ignore"),
        ('    pytest.skip("no docker")', "pytest.skip"),
        ("@pytest.mark.skip(reason='flaky')", "pytest.mark.skip"),
        ("@pytest.mark.skip", "pytest.mark.skip"),
        ("@pytest.mark.xfail(strict=True)", "xfail"),
        ('    pytest.xfail("known")', "xfail"),
        ("@pytest.mark.skipif(sys.platform == 'win32', reason='posix')", "skipif"),
        ('test.skip(!process.env.E2E, "needs a server");', "test.skip"),
        ('it.skip("later", () => {});', "it.skip"),
        ('describe.skip("later", () => {});', "describe.skip"),
    ],
)
def test_each_spelling_counts_once_under_its_kind(line: str, kind: str) -> None:
    counts = ratchet.count_text(line)
    assert counts[kind] == 1
    assert sum(counts.values()) == 1, counts


def test_ordinary_code_counts_nothing() -> None:
    text = "skip_list = []\ndef test_it_skips(): ...\ntype_ignored = 'type' + 'ignore'\n"
    assert sum(ratchet.count_text(text).values()) == 0


def test_only_tracked_source_files_outside_the_ratchet_itself_are_counted(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("x = 1  # noqa\n")
    (tmp_path / "b.md").write_text("use `# noqa` sparingly\n")
    (tmp_path / "backend/scripts").mkdir(parents=True)
    (tmp_path / "backend/scripts/quality_ratchet.py").write_text("# noqa\n")
    paths = ["a.py", "b.md", "backend/scripts/quality_ratchet.py", "deleted.py"]
    assert ratchet.count_suppressions(tmp_path, paths)["noqa"] == 1


def _counts(noqa: int = 0) -> dict[str, int]:
    return {**dict.fromkeys(ratchet.PATTERNS, 0), "noqa": noqa}


def _lock(tests: int = 10, noqa: int = 0) -> dict[str, object]:
    return {"suppressions": _counts(noqa), "tests_executed": tests}


def test_counts_equal_to_the_lock_pass() -> None:
    assert ratchet.compare(_lock(noqa=3), _counts(noqa=3), tests=10) == []


def test_a_new_suppression_fails() -> None:
    [error] = ratchet.compare(_lock(noqa=3), _counts(noqa=4), tests=None)
    assert "new suppression" in error


def test_a_removed_suppression_must_lower_the_lock() -> None:
    [error] = ratchet.compare(_lock(noqa=3), _counts(noqa=2), tests=None)
    assert "lower the lock to 2" in error


def test_fewer_executed_tests_fail_and_more_pass() -> None:
    [error] = ratchet.compare(_lock(tests=10), _counts(), tests=9)
    assert "tests removed" in error
    assert ratchet.compare(_lock(tests=10), _counts(), tests=11) == []


def test_lock_kinds_must_match_the_counted_kinds() -> None:
    lock = _lock()
    suppressions = lock["suppressions"]
    assert isinstance(suppressions, dict)
    del suppressions["xfail"]
    assert any("lock kinds" in e for e in ratchet.compare(lock, _counts(), tests=None))


def test_executed_tests_exclude_skipped_cases(tmp_path: Path) -> None:
    (tmp_path / "junit-a").mkdir()
    (tmp_path / "junit-a/a.xml").write_text(
        "<testsuites><testsuite>"
        "<testcase name='ran'/><testcase name='failed'><failure/></testcase>"
        "<testcase name='skipped'><skipped/></testcase>"
        "</testsuite></testsuites>"
    )
    (tmp_path / "b.xml").write_text("<testsuite><testcase name='ran'/></testsuite>")
    assert ratchet.executed_tests(tmp_path) == 3


def test_missing_junit_reports_are_an_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no JUnit reports"):
        ratchet.executed_tests(tmp_path)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    _git(tmp_path, "config", "user.name", "ci")
    _write_lock(tmp_path, _lock(tests=10, noqa=3))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "tag", "base")
    return tmp_path


def _write_lock(repo: Path, lock: dict[str, object]) -> None:
    path = repo / ratchet.LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock))


def _commit(repo: Path, lock: dict[str, object], message: str) -> dict[str, object]:
    _write_lock(repo, lock)
    _git(repo, "add", ratchet.LOCK_PATH)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return lock


def test_tightening_needs_no_trailer(repo: Path) -> None:
    head = _commit(repo, _lock(tests=12, noqa=2), "chore: drop a noqa")
    assert ratchet.check_loosening(repo, "base", head) == []


def test_loosening_without_a_reason_trailer_fails(repo: Path) -> None:
    head = _commit(repo, _lock(tests=10, noqa=4), "feat: add a noqa\n\nRatchet-Reason:")
    [error] = ratchet.check_loosening(repo, "base", head)
    assert "noqa: 3 -> 4" in error
    assert "Ratchet-Reason" in error


def test_loosening_with_a_reason_trailer_passes(repo: Path) -> None:
    head = _commit(
        repo, _lock(tests=10, noqa=4),
        "feat: add a noqa\n\nRatchet-Reason: vendored module keeps upstream names",
    )
    assert ratchet.check_loosening(repo, "base", head) == []


def test_fewer_tests_also_need_the_removed_test_names(repo: Path) -> None:
    head = _commit(
        repo, _lock(tests=9, noqa=3),
        "refactor: drop the legacy exporter\n\nRatchet-Reason: feature removed",
    )
    [error] = ratchet.check_loosening(repo, "base", head)
    assert "Removed-Test" in error

    head = _commit(
        repo, _lock(tests=9, noqa=3),
        "test: name the removed test\n\n"
        "Removed-Test: tests/modules/test_exporter.py::test_legacy_csv",
    )
    assert ratchet.check_loosening(repo, "base", head) == []


def test_each_removed_test_needs_its_own_trailer(repo: Path) -> None:
    head = _commit(
        repo, _lock(tests=8, noqa=3),
        "refactor: drop two tests\n\nRatchet-Reason: feature removed\n"
        "Removed-Test: tests/test_a.py::test_one",
    )
    [error] = ratchet.check_loosening(repo, "base", head)
    assert "went down by 2 but only 1" in error


def test_an_unknown_base_ref_is_an_error_not_a_new_lock(repo: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        ratchet.check_loosening(repo, "no-such-ref", _lock(tests=1, noqa=99))


def test_a_lock_new_in_this_change_has_nothing_to_loosen(repo: Path) -> None:
    _git(repo, "rm", "-q", ratchet.LOCK_PATH)
    _git(repo, "commit", "-q", "-m", "drop lock")
    _git(repo, "tag", "-f", "base")
    head = _commit(repo, _lock(tests=1, noqa=99), "ci: add lock")
    assert ratchet.check_loosening(repo, "base", head) == []


def test_the_committed_lock_matches_the_repository() -> None:
    lock = json.loads((BACKEND.parent / ratchet.LOCK_PATH).read_text())
    counts = ratchet.count_suppressions(ratchet.REPO, ratchet.tracked_files(ratchet.REPO))
    assert ratchet.compare(lock, counts, tests=None) == []
