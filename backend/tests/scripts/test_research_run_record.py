"""The code version a research run records, and where the record lands."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import research_run_record
from scripts.research_run_record import UNKNOWN_VERSION, record_path, resolve_code_version


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
