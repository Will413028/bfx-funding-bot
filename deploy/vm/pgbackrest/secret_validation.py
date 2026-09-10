#!/usr/bin/env python3
"""Validate the VM-only pgBackRest secret configuration boundary."""

from __future__ import annotations

import argparse
import re
import stat
import sys
from collections.abc import Sequence
from os import stat_result
from pathlib import Path
from typing import NoReturn

REQUIRED_OPTIONS: tuple[str, ...] = (
    "repo1-s3-endpoint",
    "repo1-s3-bucket",
    "repo1-s3-key",
    "repo1-s3-key-secret",
    "repo1-cipher-pass",
)

_ERROR = "secret_config_invalid"
_EXAMPLE_MARKER = re.compile(
    r"example|placeholder|change[-_ ]?me|replace[-_ ]?me|<[^>]+>", re.IGNORECASE
)
_UNSAFE_MODE = stat.S_IWGRP | stat.S_IWOTH | stat.S_IROTH


class SecretConfigError(ValueError):
    """The VM pgBackRest secret boundary is not safe to mount."""


def _invalid() -> NoReturn:
    raise SecretConfigError(_ERROR) from None


def _validate_permissions(
    path_stat: stat_result,
    *,
    postgres_uid: int,
    postgres_gid: int,
    directory: bool,
) -> None:
    mode = path_stat.st_mode
    owner = path_stat.st_uid
    group = path_stat.st_gid
    group_access = stat.S_IRGRP | (stat.S_IXGRP if directory else 0)
    owner_access = stat.S_IRUSR | (stat.S_IXUSR if directory else 0)

    if mode & _UNSAFE_MODE:
        _invalid()
    if mode & group_access and group != postgres_gid:
        _invalid()
    if owner == postgres_uid:
        if mode & owner_access != owner_access:
            _invalid()
    elif group == postgres_gid:
        if mode & group_access != group_access:
            _invalid()
    else:
        _invalid()


def _parse_file(path: Path, seen: set[str]) -> None:
    try:
        raw_content = path.read_bytes()
        if b"\r" in raw_content.replace(b"\r\n", b""):
            _invalid()
        content = raw_content.decode("utf-8")
    except (OSError, UnicodeError):
        _invalid()
    # Python splitlines accepts separators that the pgBackRest INI loader does
    # not. NUL/control bytes can also truncate or change an opaque secret value.
    if any(
        (ord(char) < 32 and char not in "\n\r\t")
        or char in "\x7f\x85\u2028\u2029"
        for char in content
    ):
        _invalid()
    lines = content.split("\n")
    in_global = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            if stripped != "[global]" or in_global:
                _invalid()
            in_global = True
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (
            not in_global
            or not separator
            or key not in REQUIRED_OPTIONS
            or key in seen
            or not value
            or _EXAMPLE_MARKER.search(value) is not None
        ):
            _invalid()
        seen.add(key)
    if not in_global:
        _invalid()


def validate_secret_dir(
    path: Path, *, postgres_uid: int = 70, postgres_gid: int = 70,
) -> tuple[Path, ...]:
    """Validate the exact five secret options and return file paths only."""
    try:
        directory_stat = path.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            _invalid()
        _validate_permissions(
            directory_stat,
            postgres_uid=postgres_uid,
            postgres_gid=postgres_gid,
            directory=True,
        )
        entries = sorted(path.iterdir())
    except SecretConfigError:
        raise
    except OSError:
        _invalid()

    seen: set[str] = set()
    files: list[Path] = []
    for entry in entries:
        try:
            entry_stat = entry.lstat()
        except OSError:
            _invalid()
        if (
            stat.S_ISLNK(entry_stat.st_mode)
            or not stat.S_ISREG(entry_stat.st_mode)
            or entry.suffix != ".conf"
        ):
            _invalid()
        _validate_permissions(
            entry_stat,
            postgres_uid=postgres_uid,
            postgres_gid=postgres_gid,
            directory=False,
        )
        _parse_file(entry, seen)
        files.append(entry)

    if set(REQUIRED_OPTIONS) != seen:
        _invalid()
    return tuple(files)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _invalid()


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--secret-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        validate_secret_dir(args.secret_dir)
    except SecretConfigError:
        print(_ERROR, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
