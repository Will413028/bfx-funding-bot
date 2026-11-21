"""The code version a research run records, and where the record lands."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from scripts import research_run_record
from scripts.research_run_record import (
    UNKNOWN_VERSION,
    RunStart,
    record_path,
    registry_entry_arg,
    report_path_arg,
    resolve_code_version,
)


def test_the_image_commit_wins() -> None:
    assert resolve_code_version({"BFX_SERVICE_VERSION": " abc123 "}) == "abc123"


def test_without_an_image_commit_the_checkout_head_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research_run_record, "_git",
                        lambda *args: "f00d" if args[0] == "rev-parse" else "")
    assert resolve_code_version({}) == "f00d"
    # An image built without a commit carries the literal "unknown": fall through to git.
    assert resolve_code_version({"BFX_SERVICE_VERSION": "unknown"}) == "f00d"


def test_uncommitted_changes_mark_the_version_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research_run_record, "_git",
                        lambda *args: "f00d" if args[0] == "rev-parse" else " M backend/x.py")
    assert resolve_code_version({}) == "f00d-dirty"


def test_no_image_commit_and_no_git_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*_args: str) -> str:
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(research_run_record, "_git", no_git)
    assert resolve_code_version({}) == UNKNOWN_VERSION


def test_the_record_sits_beside_the_report() -> None:
    assert record_path(Path("/reports/2026-11-21-search.md")) == Path("/reports/2026-11-21-search.run.json")


def test_the_dirty_check_covers_untracked_files_under_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def git(*args: str) -> str:
        calls.append(args)
        return "f00d" if args[0] == "rev-parse" else "?? backend/src/new_module.py"

    monkeypatch.setattr(research_run_record, "_git", git)
    assert resolve_code_version({}) == "f00d-dirty"
    assert calls[1] == ("status", "--porcelain", "--", "backend")


def test_the_code_version_is_fixed_when_the_run_begins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(research_run_record, "resolve_code_version", lambda: "commit-a")
    start = RunStart.begin("entry", "runner")
    monkeypatch.setattr(research_run_record, "resolve_code_version", lambda: "commit-b")
    assert start.code_version == "commit-a"
    assert start.started_at.tzinfo is not None


@pytest.mark.parametrize("raw", ["", " grid-3 ", "grid-3\n"])
def test_a_blank_or_padded_registry_entry_is_refused_at_parse_time(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        registry_entry_arg(raw)


def test_an_output_that_is_its_own_json_sibling_is_refused() -> None:
    assert report_path_arg("/reports/search.md") == Path("/reports/search.md")
    with pytest.raises(argparse.ArgumentTypeError):
        report_path_arg("/reports/search.json")
