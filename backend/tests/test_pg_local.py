"""Locating and validating the PostgreSQL binaries the DB tests run on (no server needed)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import pg_local

DIGEST = "sha256:" + "a" * 64


def test_the_major_comes_from_the_dockerfile_from_line() -> None:
    assert pg_local.dockerfile_pg_major(f"FROM postgres:17-alpine@{DIGEST} AS build\n") == 17
    assert pg_local.postgres_image_reference(f"# c\nFROM postgres:18-alpine@{DIGEST}\n") == (
        f"postgres:18-alpine@{DIGEST}"
    )


def test_a_dockerfile_without_a_pinned_postgres_base_is_an_error_not_a_default() -> None:
    with pytest.raises(ValueError, match="no `FROM postgres"):
        pg_local.dockerfile_pg_major("FROM alpine:3.20\n")
    with pytest.raises(ValueError, match="no `FROM postgres"):
        pg_local.dockerfile_pg_major("FROM postgres:18-alpine\n")


def test_the_repository_dockerfile_pins_postgres_18() -> None:
    assert pg_local.dockerfile_pg_major() == 18


def _bin(tmp_path: Path, name: str = "bin") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "pg_ctl").write_text("")
    return directory


def test_explicit_environment_wins_and_a_wrong_path_is_an_error(tmp_path: Path) -> None:
    good = _bin(tmp_path)
    assert pg_local.find_pg_bin({pg_local.BIN_ENV: str(good)}, homebrew=tmp_path / "none", major=18) == good
    with pytest.raises(pg_local.PgBinNotFoundError, match="BFX_TEST_PG_BIN"):
        pg_local.find_pg_bin({pg_local.BIN_ENV: str(tmp_path / "typo")}, homebrew=good, major=18)


def test_homebrew_keg_is_used_when_the_environment_is_unset(tmp_path: Path) -> None:
    keg = _bin(tmp_path)
    assert pg_local.find_pg_bin({}, homebrew=keg, major=18) == keg


def test_nothing_found_is_none_and_the_message_says_how_to_install(tmp_path: Path) -> None:
    assert pg_local.find_pg_bin({}, homebrew=tmp_path / "none", pg_config="/nonexistent/pg_config",
                                major=999) is None
    message = pg_local.missing_bin_message(18)
    assert "brew install postgresql@18" in message and "PGDG" in message
    assert "never fall back to Docker" in message


def test_a_different_major_is_refused_with_the_dockerfile_as_the_reference() -> None:
    pg_local.check_major(18, 18, "x")
    with pytest.raises(pg_local.PgVersionMismatchError, match=r"PostgreSQL 16.*pins postgres:18"):
        pg_local.check_major(16, 18, "/some/pg_ctl")
