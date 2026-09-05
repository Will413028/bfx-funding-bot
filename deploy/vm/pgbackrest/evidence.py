#!/usr/bin/env python3
"""Render bounded, secret-free pgBackRest backup and restore evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

BACKUP_ERROR_CODES = frozenset(
    {
        "archive_never_confirmed",
        "archive_lag_exceeded",
        "backup_set_missing",
        "archiver_output_invalid",
        "pgbackrest_info_invalid",
        "pgbackrest_check_failed",
        "config_unreadable",
    }
)
RESTORE_ERROR_CODES = frozenset(
    {
        "restore_output_invalid",
        "schema_output_invalid",
        "event_hash_invalid",
        "projection_replay_mismatch",
        "network_not_internal",
        "rto_invalid",
        "restore_command_failed",
        "cleanup_failed",
    }
)

_BACKUP_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_NETWORK_NAME = re.compile(r"bfx-dr-[a-z0-9-]+")
_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
_WAL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
_PROJECTION_NAMES = (
    "offer_claims",
    "position_state",
    "venue_offer_state",
    "venue_credit_state",
    "projection_heads",
    "reconcile_observation",
    "submission_attempts",
    "execution_uncertainties",
)
_REPORT_TABLE_NAMES = ("event_log", *_PROJECTION_NAMES)
_DATABASE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_TARGET_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_BASELINE_MAX_BYTES = 64 * 1024


class EvidenceError(ValueError):
    """Input is unavailable, malformed, stale, or fails a DR gate."""


def _raise(code: str) -> None:
    raise EvidenceError(code)


@dataclass(frozen=True, slots=True)
class RestoreBaseline:
    target_backup_label: str
    target_time: str | None
    database_name: str
    account_id: str
    environment: str
    projector_version: str
    migration_heads: tuple[str, ...]
    event_count: int
    event_head: int | None
    event_hash: str

    def __post_init__(self) -> None:
        code = "restore_output_invalid"
        for value, pattern in (
            (self.target_backup_label, _BACKUP_LABEL),
            (self.database_name, _DATABASE_NAME),
            (self.environment, _IDENTIFIER),
            (self.projector_version, _IDENTIFIER),
        ):
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                _raise(code)
        if self.environment not in {"prod", "shadow", "ci"}:
            _raise(code)
        if self.target_time is not None:
            if not isinstance(self.target_time, str) or _TARGET_TIME.fullmatch(self.target_time) is None:
                _raise(code)
            try:
                datetime.strptime(self.target_time, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                _raise(code)
        try:
            if not isinstance(self.account_id, str) or str(UUID(self.account_id)) != self.account_id:
                _raise(code)
        except ValueError:
            _raise(code)
        if (
            not isinstance(self.migration_heads, tuple)
            or not 1 <= len(self.migration_heads) <= 32
            or any(not isinstance(head, str) or _IDENTIFIER.fullmatch(head) is None
                   for head in self.migration_heads)
            or len(set(self.migration_heads)) != len(self.migration_heads)
        ):
            _raise(code)
        if type(self.event_count) is not int or self.event_count < 0:
            _raise(code)
        if self.event_head is None:
            if self.event_count != 0:
                _raise(code)
        elif type(self.event_head) is not int or self.event_head < 0 or self.event_count == 0:
            _raise(code)
        if not _is_sha256(self.event_hash):
            _raise("event_hash_invalid")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _raise("restore_output_invalid")
        result[key] = value
    return result


def load_restore_baseline(
    path: Path, *, target_backup_label: str, target_time: str | None,
    account_id: str, environment: str, projector_version: str,
) -> RestoreBaseline:
    """Read only a bounded regular JSON file and require exact request identity."""
    code = "restore_output_invalid"
    if not path.is_absolute():
        _raise(code)
    try:
        # O_NONBLOCK prevents FIFOs from hanging; fstat checks the opened artifact.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _BASELINE_MAX_BYTES:
                _raise(code)
            raw = handle.read(_BASELINE_MAX_BYTES + 1)
        if len(raw) > _BASELINE_MAX_BYTES:
            _raise(code)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, ValueError, RecursionError):
        _raise(code)
    if not isinstance(payload, dict) or set(payload) != {field.name for field in fields(RestoreBaseline)}:
        _raise(code)
    if not isinstance(payload["migration_heads"], list):
        _raise(code)
    payload["migration_heads"] = tuple(payload["migration_heads"])
    baseline = RestoreBaseline(**payload)
    for name, expected in (
        ("target_backup_label", target_backup_label), ("target_time", target_time),
        ("account_id", account_id), ("environment", environment),
        ("projector_version", projector_version),
    ):
        if getattr(baseline, name) != expected:
            _raise(code)
    return baseline


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
        if not isinstance(start, dict) or not isinstance(stop, dict):
            _raise("pgbackrest_info_invalid")
        start_epoch = _nonnegative_int(start.get("epoch"), code="pgbackrest_info_invalid")
        stop_epoch = _nonnegative_int(stop.get("epoch"), code="pgbackrest_info_invalid")
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


def _parse_schema(schema_tsv: str) -> tuple[int, list[str], int]:
    row = schema_tsv.rstrip("\r\n")
    if "\n" in row or "\r" in row:
        _raise("schema_output_invalid")
    fields = row.split("\t")
    if len(fields) != 3:
        _raise("schema_output_invalid")
    server_version_num = _nonnegative_int(fields[0], code="schema_output_invalid")
    event_count = _nonnegative_int(fields[2], code="schema_output_invalid")
    migration_heads = fields[1].split(",")
    if (
        server_version_num == 0
        or not migration_heads
        or len(migration_heads) > 32
        or any(_IDENTIFIER.fullmatch(head) is None for head in migration_heads)
    ):
        _raise("schema_output_invalid")
    return server_version_num, migration_heads, event_count


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _parse_replay(replay_json: str, *, event_count: int) -> dict[str, object]:
    try:
        payload = json.loads(replay_json)
    except (json.JSONDecodeError, TypeError):
        _raise("restore_output_invalid")
    if not isinstance(payload, dict):
        _raise("restore_output_invalid")

    account_id = payload.get("account_id")
    environment = payload.get("environment")
    projector_version = payload.get("projector_version")
    try:
        canonical_account_id = str(UUID(account_id)) if isinstance(account_id, str) else ""
    except ValueError:
        canonical_account_id = ""
    if (
        account_id != canonical_account_id
        or not isinstance(environment, str)
        or _IDENTIFIER.fullmatch(environment) is None
        or not isinstance(projector_version, str)
        or _IDENTIFIER.fullmatch(projector_version) is None
    ):
        _raise("restore_output_invalid")

    event_hash = payload.get("event_hash")
    if not _is_sha256(event_hash):
        _raise("event_hash_invalid")
    event_head_raw = payload.get("event_head")
    if event_head_raw is None and event_count == 0:
        event_head: int | None = None
    else:
        if type(event_head_raw) is not int:
            _raise("restore_output_invalid")
        event_head = _nonnegative_int(event_head_raw, code="restore_output_invalid")

    row_counts_raw = payload.get("row_counts")
    hashes_raw = payload.get("content_hashes")
    diagnostic_raw = payload.get("diagnostic_diff")
    if not all(isinstance(value, dict) for value in (row_counts_raw, hashes_raw, diagnostic_raw)):
        _raise("restore_output_invalid")
    row_counts = row_counts_raw
    hashes = hashes_raw
    diagnostics = diagnostic_raw
    bounded_counts: dict[str, int] = {}
    bounded_hashes: dict[str, str] = {}
    for name in _REPORT_TABLE_NAMES:
        count = row_counts.get(name)
        digest = hashes.get(name)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _raise("restore_output_invalid")
        if not _is_sha256(digest):
            _raise("event_hash_invalid")
        bounded_counts[name] = count
        bounded_hashes[name] = digest
    if bounded_counts["event_log"] != event_count or bounded_hashes["event_log"] != event_hash:
        _raise("event_hash_invalid")

    for name in _PROJECTION_NAMES:
        diagnostic = diagnostics.get(name)
        if not isinstance(diagnostic, dict) or diagnostic.get("matches") is not True:
            _raise("projection_replay_mismatch")
        old_count = diagnostic.get("old_count")
        replayed_count = diagnostic.get("replayed_count")
        old_hash = diagnostic.get("old_hash")
        replayed_hash = diagnostic.get("replayed_hash")
        if (
            isinstance(old_count, bool)
            or not isinstance(old_count, int)
            or type(replayed_count) is not int
            or old_count < 0
            or old_count != replayed_count
            or replayed_count != bounded_counts[name]
            or not _is_sha256(old_hash)
            or old_hash != replayed_hash
            or replayed_hash != bounded_hashes[name]
        ):
            _raise("projection_replay_mismatch")

    return {
        "account_id": account_id,
        "environment": environment,
        "projector_version": projector_version,
        "event_head": event_head,
        "event_hash": event_hash,
        "row_counts": bounded_counts,
        "projection_hashes": {
            name: bounded_hashes[name] for name in _PROJECTION_NAMES
        },
    }


def render_restore_evidence(
    *,
    schema_tsv: str,
    replay_json: str,
    baseline: RestoreBaseline,
    elapsed_seconds: int,
    observed_at_ms: int,
    config_path: Path,
    image_digest: str,
    network_name: str,
    network_internal: bool,
    egress_disconnected: bool,
) -> dict[str, object]:
    """Return bounded measured restore evidence or raise EvidenceError."""
    if (
        isinstance(elapsed_seconds, bool)
        or not isinstance(elapsed_seconds, int)
        or elapsed_seconds < 0
    ):
        _raise("rto_invalid")
    if (
        network_internal is not True
        or len(network_name) > 128
        or _NETWORK_NAME.fullmatch(network_name) is None
    ):
        _raise("network_not_internal")
    if type(observed_at_ms) is not int or observed_at_ms < 0 or egress_disconnected is not True:
        _raise("restore_output_invalid")
    baseline.__post_init__()
    if _SHA256.fullmatch(image_digest) is None:
        _raise("restore_output_invalid")

    server_version_num, migration_heads, event_count = _parse_schema(schema_tsv)
    replay = _parse_replay(replay_json, event_count=event_count)
    if (
        sorted(migration_heads) != sorted(baseline.migration_heads)
        or event_count != baseline.event_count
        or any(replay[name] != getattr(baseline, name) for name in (
            "account_id", "environment", "projector_version", "event_head", "event_hash"
        ))
    ):
        _raise("restore_output_invalid")
    return {
        "schema_version": 1,
        "measured": True,
        "rto_seconds": elapsed_seconds,
        "target_backup_label": baseline.target_backup_label,
        "target_time": baseline.target_time,
        "observed_at_ms": observed_at_ms,
        "egress_disconnected": True,
        "server_version_num": server_version_num,
        "migration_heads": migration_heads,
        "event_count": event_count,
        **replay,
        "network_name": network_name,
        "network_internal": True,
        "verifier_exit_status": 0,
        "config_digest": _config_digest(config_path, code="restore_output_invalid"),
        "image_digest": image_digest,
    }


def render_failure_evidence(
    *,
    kind: Literal["backup", "restore"],
    error_code: str,
    observed_at_ms: int,
) -> dict[str, object]:
    """Return a measured=false report with only a bounded error code."""
    allowlist = BACKUP_ERROR_CODES if kind == "backup" else RESTORE_ERROR_CODES
    if kind not in {"backup", "restore"} or error_code not in allowlist:
        _raise("archiver_output_invalid" if kind == "backup" else "restore_output_invalid")
    observed = _nonnegative_int(observed_at_ms, code="archiver_output_invalid")
    return {
        "schema_version": 1,
        "measured": False,
        "kind": kind,
        "observed_at_ms": observed,
        "error_code": error_code,
    }


def _atomic_write_json(path: Path, report: dict[str, object]) -> None:
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

    backup = subparsers.add_parser("backup")
    backup.add_argument("--archiver-tsv", type=Path, required=True)
    backup.add_argument("--info-json", type=Path, required=True)
    backup.add_argument("--config", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--rpo-limit-seconds", type=int, default=300)

    backup_failure = subparsers.add_parser("backup-failure")
    backup_failure.add_argument("--error-code", required=True)
    backup_failure.add_argument("--output", type=Path, required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--schema-tsv", type=Path, required=True)
    restore.add_argument("--replay-json", type=Path, required=True)
    restore.add_argument("--target-backup-label", required=True)
    restore.add_argument("--target-time")
    restore.add_argument("--baseline", type=Path, required=True)
    restore.add_argument("--account-id", required=True)
    restore.add_argument("--environment", required=True)
    restore.add_argument("--projector-version", required=True)
    restore.add_argument("--observed-at-ms", type=int, required=True)
    restore.add_argument("--egress-disconnected", action="store_true")
    restore.add_argument("--elapsed-seconds", type=int, required=True)
    restore.add_argument("--config", type=Path, required=True)
    restore.add_argument("--image-digest", required=True)
    restore.add_argument("--network-name", required=True)
    restore.add_argument("--network-internal", action="store_true")
    restore.add_argument("--output", type=Path, required=True)
    return parser


def _paths_are_absolute(args: argparse.Namespace) -> bool:
    names = ("config", "output", "archiver_tsv", "info_json", "schema_tsv", "replay_json", "baseline")
    return all(
        not hasattr(args, name) or getattr(args, name).is_absolute()
        for name in names
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not _paths_are_absolute(args):
        return 2

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
        if args.kind == "backup":
            archiver_tsv = _read_input(args.archiver_tsv, code="archiver_output_invalid")
            observed_at_ms = _safe_observed_at_ms(archiver_tsv)
            report = render_backup_evidence(
                archiver_tsv=archiver_tsv,
                info_json=_read_input(args.info_json, code="pgbackrest_info_invalid"),
                config_path=args.config,
                rpo_limit_seconds=args.rpo_limit_seconds,
            )
        else:
            report = render_restore_evidence(
                schema_tsv=_read_input(args.schema_tsv, code="schema_output_invalid"),
                replay_json=_read_input(args.replay_json, code="restore_output_invalid"),
                baseline=load_restore_baseline(
                    args.baseline, target_backup_label=args.target_backup_label,
                    target_time=args.target_time, account_id=args.account_id,
                    environment=args.environment, projector_version=args.projector_version,
                ),
                observed_at_ms=args.observed_at_ms,
                egress_disconnected=args.egress_disconnected,
                elapsed_seconds=args.elapsed_seconds,
                config_path=args.config,
                image_digest=args.image_digest,
                network_name=args.network_name,
                network_internal=args.network_internal,
            )
    except EvidenceError as exc:
        code = str(exc)
        allowlist = BACKUP_ERROR_CODES if args.kind == "backup" else RESTORE_ERROR_CODES
        if code not in allowlist:
            code = "archiver_output_invalid" if args.kind == "backup" else "restore_output_invalid"
        report = render_failure_evidence(
            kind=args.kind, error_code=code, observed_at_ms=observed_at_ms
        )
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
