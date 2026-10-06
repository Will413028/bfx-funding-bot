#!/usr/bin/env python3
"""Render bounded, secret-free pgBackRest backup and restore evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import ModuleType
from typing import Literal


def _load_ledger_digest() -> ModuleType:
    """ledger_digest.py, registered so its dataclasses resolve and every loader shares it."""
    name = "_bfx_ledger_digest"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("ledger_digest.py"))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_ledger = _load_ledger_digest()

BACKUP_ERROR_CODES = frozenset(
    {
        "archive_never_confirmed",
        "archive_lag_exceeded",
        "backup_set_missing",
        "archiver_output_invalid",
        "pgbackrest_info_invalid",
        "pgbackrest_check_failed",
        "backup_refresh_incomplete",
        "config_unreadable",
    }
)
RESTORE_ERROR_CODES = frozenset(
    {
        "restore_output_invalid",
        "network_not_internal",
        "rto_invalid",
        "restore_command_failed",
        "cleanup_failed",
    }
)

# Every restore drill (restore_drill.py: the --restore-test and the operator's acceptance
# drill) verifies the ledger: no operator baseline. The restored copy's append-only ledger
# rows must equal production's within the restored copy's own boundary, and the image's
# boot guards must accept it (ledger_digest.py, ledger_boot_check.py).
LEDGER_ERROR_CODES = RESTORE_ERROR_CODES | _ledger.ERROR_CODES | frozenset(
    {"production_read_failed", "backup_label_unavailable"}
)
# The restore receipt kinds: `restore_ledger`, and `restore_prefix` only while the installed
# restore-test wrapper of the previous release still calls `--prefix`.
LEDGER_KINDS = frozenset({"restore_ledger", "restore_prefix"})

_BACKUP_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_NETWORK_NAME = re.compile(r"bfx-dr-[a-z0-9-]+")
_WAL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
_TARGET_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_MAX_EVIDENCE_AGE_MS = 900_000
_IMAGE_LABELS = {
    "org.bfx.postgresql.base-digest": "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2",
    "org.bfx.pgbackrest.version": "2.59.1",
    "org.bfx.pgbackrest.source-sha256": "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d",
}


class EvidenceError(ValueError):
    """Input is unavailable, malformed, stale, or fails a DR gate."""


def _raise(code: str) -> None:
    raise EvidenceError(code)


def is_image_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _raise("restore_output_invalid")
        result[key] = value
    return result


def _nonnegative_int(value: object, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _raise(code)
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
        _raise(code)
    try:
        parsed = int(value)
    except ValueError:
        _raise(code)
    if parsed < 0:
        _raise(code)
    return parsed


def _optional_nonnegative_int(value: str, *, code: str) -> int | None:
    if value == "":
        return None
    return _nonnegative_int(value, code=code)


def _config_digest(config_path: Path, *, code: str) -> str:
    try:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError:
        _raise(code)


def _parse_archiver(archiver_tsv: str) -> tuple[int, int, str, int]:
    row = archiver_tsv.rstrip("\r\n")
    if "\n" in row or "\r" in row:
        _raise("archiver_output_invalid")
    fields = row.split("\t")
    if len(fields) != 5:
        _raise("archiver_output_invalid")
    observed_at_ms = _nonnegative_int(fields[0], code="archiver_output_invalid")
    last_archived_at_ms = _optional_nonnegative_int(
        fields[1], code="archiver_output_invalid"
    )
    wal = fields[2]
    failed_count = _nonnegative_int(fields[3], code="archiver_output_invalid")
    last_failed_at_ms = _optional_nonnegative_int(fields[4], code="archiver_output_invalid")

    if last_archived_at_ms is None:
        if wal:
            _raise("archiver_output_invalid")
        _raise("archive_never_confirmed")
    if not _WAL_NAME.fullmatch(wal) or last_archived_at_ms > observed_at_ms:
        _raise("archiver_output_invalid")
    if last_failed_at_ms is not None and last_failed_at_ms > observed_at_ms:
        _raise("archiver_output_invalid")
    if last_failed_at_ms is not None and last_failed_at_ms > last_archived_at_ms:
        _raise("archive_lag_exceeded")
    return observed_at_ms, last_archived_at_ms, wal, failed_count


def _parse_pgbackrest_info(info_json: str) -> str:
    try:
        payload = json.loads(info_json)
    except (json.JSONDecodeError, TypeError):
        _raise("pgbackrest_info_invalid")
    if not isinstance(payload, list):
        _raise("pgbackrest_info_invalid")

    stanza: dict[str, object] | None = None
    for candidate in payload:
        if not isinstance(candidate, dict):
            _raise("pgbackrest_info_invalid")
        if candidate.get("name") == "bfx":
            stanza = candidate
            break
    if stanza is None:
        _raise("backup_set_missing")
    if len(payload) != 1:
        _raise("pgbackrest_info_invalid")
    repositories = stanza.get("repo")
    if not isinstance(repositories, list) or len(repositories) != 1:
        _raise("pgbackrest_info_invalid")
    repository = repositories[0]
    if (
        not isinstance(repository, dict)
        or type(repository.get("key")) is not int
        or repository["key"] != 1
        or repository.get("cipher") != "aes-256-cbc"
    ):
        _raise("pgbackrest_info_invalid")
    for item in (stanza, repository):
        status = item.get("status")
        if (
            not isinstance(status, dict)
            or type(status.get("code")) is not int
            or status["code"] != 0
        ):
            _raise("pgbackrest_info_invalid")

    backups = stanza.get("backup")
    if not isinstance(backups, list) or not backups:
        _raise("backup_set_missing")

    parsed: list[tuple[int, str, str, int]] = []
    for backup in backups:
        if not isinstance(backup, dict):
            _raise("pgbackrest_info_invalid")
        label = backup.get("label")
        backup_type = backup.get("type")
        timestamp = backup.get("timestamp")
        if (
            not isinstance(label, str)
            or _BACKUP_LABEL.fullmatch(label) is None
            or backup_type not in {"full", "diff", "incr"}
            or not isinstance(timestamp, dict)
        ):
            _raise("pgbackrest_info_invalid")
        start = timestamp.get("start")
        stop = timestamp.get("stop")
        if type(start) is not int or type(stop) is not int:
            _raise("pgbackrest_info_invalid")
        start_epoch = _nonnegative_int(start, code="pgbackrest_info_invalid")
        stop_epoch = _nonnegative_int(stop, code="pgbackrest_info_invalid")
        if stop_epoch < start_epoch:
            _raise("pgbackrest_info_invalid")
        parsed.append((stop_epoch, label, backup_type, start_epoch))

    if not any(backup_type == "full" for _, _, backup_type, _ in parsed):
        _raise("backup_set_missing")
    return max(parsed, key=lambda item: item[0])[1]


def render_backup_evidence(
    *,
    archiver_tsv: str,
    info_json: str,
    config_path: Path,
    rpo_limit_seconds: int,
) -> dict[str, object]:
    """Return bounded measured backup evidence or raise EvidenceError."""
    if isinstance(rpo_limit_seconds, bool) or rpo_limit_seconds <= 0:
        _raise("archive_lag_exceeded")
    observed_at_ms, last_archived_at_ms, wal, failed_count = _parse_archiver(archiver_tsv)
    rpo_seconds = (observed_at_ms - last_archived_at_ms + 999) // 1000
    if rpo_seconds > rpo_limit_seconds:
        _raise("archive_lag_exceeded")
    latest_backup_label = _parse_pgbackrest_info(info_json)
    return {
        "schema_version": 1,
        "kind": "backup",
        "measured": True,
        "rpo_seconds": rpo_seconds,
        "observed_at_ms": observed_at_ms,
        "stanza": "bfx",
        "repository": "r2",
        "last_archived_wal": wal,
        "latest_backup_label": latest_backup_label,
        "config_digest": _config_digest(config_path, code="config_unreadable"),
        "failed_archive_count": failed_count,
    }


def validate_image_labels(image_labels: Mapping[str, str]) -> dict[str, str]:
    """Keep only the three exact pinned labels; arbitrary metadata is never evidence."""
    if not isinstance(image_labels, Mapping) or any(
        image_labels.get(name) != expected for name, expected in _IMAGE_LABELS.items()
    ):
        _raise("restore_output_invalid")
    return dict(_IMAGE_LABELS)


def _parse_image_labels(raw: str) -> dict[str, str]:
    try:
        labels = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, TypeError):
        _raise("restore_output_invalid")
    return validate_image_labels(labels)


def render_ledger_restore_evidence(
    *,
    kind: str,
    bounds: object,
    ledger: Mapping[str, object],
    boot: Mapping[str, object],
    target_backup_label: str,
    elapsed_seconds: int,
    observed_at_ms: int,
    config_path: Path,
    image_digest: str,
    target_time: str | None = None,
    restore_test: bool = True,
    image_labels: Mapping[str, str],
    network_name: str,
    network_internal: bool,
    egress_disconnected: bool,
    verifier_image_digest: str,
    now_ms: int | None = None,
) -> dict[str, object]:
    """Bounded evidence for a restore verified against production's ledger.

    ``restore_test`` is true for the recurring restore test (the newest backup, no target)
    and false for the operator's acceptance drill at ``target_backup_label``/``target_time``.
    """
    if kind not in LEDGER_KINDS:
        _raise("restore_output_invalid")
    if isinstance(elapsed_seconds, bool) or not isinstance(elapsed_seconds, int) or elapsed_seconds < 0:
        _raise("rto_invalid")
    if network_internal is not True or len(network_name) > 128 or _NETWORK_NAME.fullmatch(network_name) is None:
        _raise("network_not_internal")
    if type(observed_at_ms) is not int or observed_at_ms < 0 or egress_disconnected is not True:
        _raise("restore_output_invalid")
    now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if type(now) is not int or not 0 <= now - observed_at_ms <= _MAX_EVIDENCE_AGE_MS:
        _raise("restore_output_invalid")
    if not isinstance(image_digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
        _raise("restore_output_invalid")
    if not is_image_digest(verifier_image_digest):
        _raise("restore_output_invalid")
    if not isinstance(target_backup_label, str) or _BACKUP_LABEL.fullmatch(target_backup_label) is None:
        _raise("restore_output_invalid")
    if type(restore_test) is not bool or (restore_test and target_time is not None) or (
        target_time is not None and (not isinstance(target_time, str)
                                     or _TARGET_TIME.fullmatch(target_time) is None)
    ):
        _raise("restore_output_invalid")
    if not isinstance(bounds, _ledger.Bounds):
        _raise("restore_output_invalid")
    labels = validate_image_labels(image_labels)
    return {
        "schema_version": 1,
        "measured": True,
        "kind": kind,
        # The recurring restore test's mode-free contract with its caller (the wrapper).
        "restore_test": restore_test,
        "rto_seconds": elapsed_seconds,
        "target_backup_label": target_backup_label,
        "target_time": target_time,
        "observed_at_ms": observed_at_ms,
        "egress_disconnected": True,
        "server_version_num": bounds.server_version_num,
        "migration_heads": sorted(bounds.migration_heads),
        "ledger": dict(ledger),
        "boot": dict(boot),
        "network_name": network_name,
        "network_internal": True,
        "verifier_exit_status": 0,
        "verifier_image_digest": verifier_image_digest,
        "config_digest": _config_digest(config_path, code="restore_output_invalid"),
        "image_digest": image_digest,
        "image_labels": labels,
    }


def render_failure_evidence(
    *,
    kind: Literal["backup", "restore_ledger", "restore_prefix"],
    error_code: str,
    observed_at_ms: int,
    cause: str | None = None,
) -> dict[str, object]:
    """Return a measured=false report with only a bounded error code (and, for a failed
    ledger-mode cluster read, a bounded cause such as ``statement_timeout``)."""
    allowlist = BACKUP_ERROR_CODES if kind == "backup" else LEDGER_ERROR_CODES
    if kind not in {"backup", *LEDGER_KINDS} or error_code not in allowlist:
        _raise("archiver_output_invalid" if kind == "backup" else "restore_output_invalid")
    if cause is not None and (kind not in LEDGER_KINDS or cause not in _ledger.READ_FAILURE_CAUSES):
        _raise("restore_output_invalid")
    observed = _nonnegative_int(observed_at_ms, code="archiver_output_invalid")
    return {
        "schema_version": 1,
        "measured": False,
        "kind": kind,
        "observed_at_ms": observed,
        "error_code": error_code,
        **({"cause": cause} if cause is not None else {}),
    }


def _atomic_write_json(path: Path, report: dict[str, object]) -> None:
    try:
        _replace_json(path, report)
    except OSError:
        # A failed refresh must revoke any previously accepted green artifact.
        # Unlink needs no new blocks; writable-file truncation also works when
        # the directory denies unlink. Never expose filesystem exception text.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            with suppress(OSError), path.open("r+") as handle:
                handle.truncate(0)
        raise


def _replace_json(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(report, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_input(path: Path, *, code: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        _raise(code)


def _safe_observed_at_ms(archiver_tsv: str) -> int:
    first = archiver_tsv.partition("\t")[0]
    if first.isascii() and first.isdigit():
        value = int(first)
        if value >= 0:
            return value
    return time.time_ns() // 1_000_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="kind", required=True)

    smoke = subparsers.add_parser("smoke-info")
    smoke.add_argument("--info-json", type=Path, required=True)

    backup = subparsers.add_parser("backup")
    backup.add_argument("--archiver-tsv", type=Path, required=True)
    backup.add_argument("--info-json", type=Path, required=True)
    backup.add_argument("--config", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--rpo-limit-seconds", type=int, default=300)

    backup_failure = subparsers.add_parser("backup-failure")
    backup_failure.add_argument("--error-code", required=True)
    backup_failure.add_argument("--output", type=Path, required=True)

    return parser


def _paths_are_absolute(args: argparse.Namespace) -> bool:
    names = ("config", "output", "archiver_tsv", "info_json")
    return all(
        not hasattr(args, name) or getattr(args, name).is_absolute()
        for name in names
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not _paths_are_absolute(args):
        return 2
    if args.kind == "smoke-info":
        try:
            _parse_pgbackrest_info(_read_input(args.info_json, code="pgbackrest_info_invalid"))
        except EvidenceError:
            return 2
        return 0

    observed_at_ms = time.time_ns() // 1_000_000
    if args.kind == "backup-failure":
        try:
            report = render_failure_evidence(
                kind="backup",
                error_code=args.error_code,
                observed_at_ms=observed_at_ms,
            )
            _atomic_write_json(args.output, report)
        except (EvidenceError, OSError):
            return 2
        return 0

    try:
        archiver_tsv = _read_input(args.archiver_tsv, code="archiver_output_invalid")
        observed_at_ms = _safe_observed_at_ms(archiver_tsv)
        report = render_backup_evidence(
            archiver_tsv=archiver_tsv,
            info_json=_read_input(args.info_json, code="pgbackrest_info_invalid"),
            config_path=args.config,
            rpo_limit_seconds=args.rpo_limit_seconds,
        )
    except (EvidenceError, ValueError, OSError) as exc:
        code = str(exc)
        if code not in BACKUP_ERROR_CODES:
            code = "archiver_output_invalid"
        report = render_failure_evidence(kind="backup", error_code=code,
                                         observed_at_ms=observed_at_ms)
        with suppress(OSError):
            _atomic_write_json(args.output, report)
        return 2

    try:
        _atomic_write_json(args.output, report)
    except OSError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
