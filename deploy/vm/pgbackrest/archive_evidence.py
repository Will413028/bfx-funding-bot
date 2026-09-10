"""Stdlib-only opaque archive transport and bounded verifier report contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from uuid import UUID

MAX_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 65536
MAX_RUNS = 32
TABLES = ("execution_uncertainties", "offer_claims", "position_state", "projection_heads",
          "reconcile_observation", "submission_attempts", "venue_credit_state", "venue_offer_state")


def digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def image_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("restore_output_invalid")
        result[key] = value
    return result


def read_private(path: Path, *, expected_digest: str | None = None) -> bytes:
    if not path.is_absolute():
        raise ValueError("restore_output_invalid")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
        meta = os.fstat(handle.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > MAX_BYTES):
            raise ValueError("restore_output_invalid")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or (expected_digest is not None and (
        not digest(expected_digest) or hashlib.sha256(raw).hexdigest() != expected_digest
    )):
        raise ValueError("restore_output_invalid")
    return raw


def validate_references(references: object) -> None:
    if not isinstance(references, tuple) or len(references) > MAX_RUNS:
        raise ValueError("restore_output_invalid")
    seen = set()
    for item in references:
        if not isinstance(item, dict) or set(item) != {
            "run_id", "manifest_digest", "prepared_path", "prepared_digest"
        }:
            raise ValueError("restore_output_invalid")
        run_id = item["run_id"]
        if (not isinstance(run_id, str) or str(UUID(run_id)) != run_id or run_id in seen
                or not digest(item["manifest_digest"]) or not digest(item["prepared_digest"])
                or not isinstance(item["prepared_path"], str)
                or not Path(item["prepared_path"]).is_absolute()):
            raise ValueError("restore_output_invalid")
        seen.add(run_id)


def validate_target(target_run_id: str | None, references: tuple[dict[str, Any], ...]) -> None:
    if target_run_id is not None and (
        not isinstance(target_run_id, str) or str(UUID(target_run_id)) != target_run_id
        or target_run_id not in {ref["run_id"] for ref in references}
    ):
        raise ValueError("restore_output_invalid")


def transport(references: tuple[dict[str, Any], ...], *, target_run_id: str | None = None) -> bytes:
    """Host never interprets typed payloads. Their exact bytes retain Task3's pin."""
    validate_references(references)
    validate_target(target_run_id, references)
    items = []
    for ref in references:
        raw = read_private(Path(ref["prepared_path"]), expected_digest=ref["prepared_digest"])
        items.append({"sha256": ref["prepared_digest"], "payload": base64.b64encode(raw).decode("ascii")})
    envelope = {"schema_version": 1, "prepared": items}
    if target_run_id is not None:
        envelope.update(schema_version=2, target_run_id=target_run_id)
    result = json.dumps(envelope, separators=(",", ":")).encode()
    if len(result) > MAX_BYTES:
        raise ValueError("restore_output_invalid")
    return result


def validate_report(raw: str | None, *, baseline: Any, archive_only: bool,
                    target_run_id: str | None = None) -> dict[str, Any]:
    """Accept only the bounded exact inventory and independently bound identities."""
    if type(archive_only) is not bool or not isinstance(raw, str) or len(raw.encode()) > MAX_REPORT_BYTES:
        raise ValueError("restore_output_invalid")
    report = json.loads(raw, object_pairs_hook=unique)
    validate_target(target_run_id, baseline.archives or ())
    if archive_only != (target_run_id is not None):
        raise ValueError("restore_output_invalid")
    target_fields = {"target_run_id"} if archive_only else set()
    if (not isinstance(report, dict) or set(report) != {
        "schema_version", "scope", "event_count", "event_head", "event_hash",
        "migration_heads", "archives", "archive_only"
    } | target_fields or type(report["schema_version"]) is not int
            or report["schema_version"] != (2 if archive_only else 1)
            or report.get("target_run_id") != target_run_id
            or type(report["archive_only"]) is not bool or report["archive_only"] != archive_only
            or report["scope"] != {"account_id": baseline.account_id, "environment": baseline.environment}
            or type(report["event_count"]) is not int or report["event_count"] != baseline.event_count
            or report["event_head"] != baseline.event_head
            or (report["event_head"] is not None and type(report["event_head"]) is not int)
            or report["event_hash"] != baseline.event_hash
            or report["migration_heads"] != sorted(baseline.migration_heads)
            or not isinstance(report["archives"], list) or len(report["archives"]) > MAX_RUNS):
        raise ValueError("restore_output_invalid")
    references = baseline.archives or ()
    expected = {ref["run_id"]: ref for ref in references}
    seen = set()
    if archive_only and not expected:
        raise ValueError("restore_output_invalid")
    for item in report["archives"]:
        if not isinstance(item, dict) or set(item) != {
            "scope", "run_id", "manifest_digest", "verified_tables", "verified_counts",
            "verified_digests", "original_event_count", "original_event_head", "original_event_hash",
            "image_digest", "projector_version", "migration_heads", "prepared_digest"
        }:
            raise ValueError("restore_output_invalid")
        run_id = item["run_id"]
        if not isinstance(run_id, str) or run_id not in expected or run_id in seen:
            raise ValueError("restore_output_invalid")
        ref = expected[run_id]
        if (item["scope"] != report["scope"] or item["manifest_digest"] != ref["manifest_digest"]
                or item["prepared_digest"] != ref["prepared_digest"]
                or item["verified_tables"] != list(TABLES)
                or not isinstance(item["verified_counts"], dict) or set(item["verified_counts"]) != set(TABLES)
                or not isinstance(item["verified_digests"], dict) or set(item["verified_digests"]) != set(TABLES)
                or any(type(c) is not int or c < 0 for c in item["verified_counts"].values())
                or any(not digest(d) for d in item["verified_digests"].values())
                or not image_digest(item["image_digest"])
                or not isinstance(item["projector_version"], str)
                or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", item["projector_version"]) is None
                or not isinstance(item["migration_heads"], list)
                or not 1 <= len(item["migration_heads"]) <= 32
                or any(not isinstance(h, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", h) is None
                       for h in item["migration_heads"])
                or item["migration_heads"] != sorted(set(item["migration_heads"]))
                or type(item["original_event_count"]) is not int or item["original_event_count"] < 0
                or type(item["original_event_head"]) is not int or item["original_event_head"] < 0
                or item["original_event_count"] > baseline.event_count
                or not item["original_event_count"] <= item["original_event_head"] <= (baseline.event_head or 0)
                or (item["original_event_count"] == 0 and item["original_event_head"] != 0)
                or not digest(item["original_event_hash"])):
            raise ValueError("restore_output_invalid")
        if run_id == target_run_id and (item["original_event_count"] != baseline.event_count
                or item["original_event_head"] != (baseline.event_head or 0)
                or item["original_event_hash"] != baseline.event_hash
                or item["image_digest"] != baseline.verifier_image_digest
                or item["projector_version"] != baseline.projector_version
                or item["migration_heads"] != sorted(baseline.migration_heads)):
            raise ValueError("restore_output_invalid")
        seen.add(run_id)
    if seen != set(expected):
        raise ValueError("restore_output_invalid")
    return report
