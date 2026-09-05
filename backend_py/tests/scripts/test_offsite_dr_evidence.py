"""Offline contracts for bounded pgBackRest DR evidence."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_PATH = ROOT / "deploy/vm/pgbackrest/evidence.py"
TOKEN_SENTINEL = "TOKEN-SENTINEL"
IMAGE_LABELS = {
    "org.bfx.postgresql.base-digest": "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2",
    "org.bfx.pgbackrest.version": "2.59.1",
    "org.bfx.pgbackrest.source-sha256": "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d",
}
PROJECTION_NAMES = (
    "offer_claims",
    "position_state",
    "venue_offer_state",
    "venue_credit_state",
    "projection_heads",
    "reconcile_observation",
    "submission_attempts",
    "execution_uncertainties",
)


def _load_evidence() -> ModuleType:
    spec = importlib.util.spec_from_file_location("offsite_dr_evidence", EVIDENCE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load evidence module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evidence = _load_evidence()
EvidenceError = evidence.EvidenceError
render_backup_evidence = evidence.render_backup_evidence
render_failure_evidence = evidence.render_failure_evidence
render_restore_evidence = evidence.render_restore_evidence


def _baseline_payload() -> dict[str, object]:
    return {
        "target_backup_label": "20260904031700-F",
        "target_time": None,
        "database_name": "bfx",
        "account_id": "3f19d046-5030-494c-9a0a-9573bb890c1f",
        "environment": "prod",
        "projector_version": "projector-v3",
        "migration_heads": ["head-a", "head-b"],
        "event_count": 9,
        "event_head": 42,
        "event_hash": "a" * 64,
    }


def _load_baseline(path: Path, **identity: object):
    request = {key: value for key, value in _baseline_payload().items() if key in (
        "target_backup_label", "target_time", "account_id", "environment", "projector_version"
    )}
    return evidence.load_restore_baseline(path, **(request | identity))


def _baseline():
    return evidence.RestoreBaseline(**(_baseline_payload() | {"migration_heads": ("head-a", "head-b")}))


@pytest.mark.parametrize(("field", "value"), [
    ("target_backup_label", "bad;label"), ("target_time", "2026-02-30T00:00:00Z"),
    ("target_time", 0), ("database_name", "bfx;DROP"), ("database_name", "x" * 64),
    ("database_name", None), ("account_id", "not-a-uuid"),
    ("account_id", "3F19D046-5030-494C-9A0A-9573BB890C1F"),
    ("environment", "prod\n"), ("environment", []), ("projector_version", ""),
    ("projector_version", "x" * 129), ("migration_heads", []),
    ("migration_heads", ["head-a", "head-a"]), ("migration_heads", ["bad;head"]),
    ("migration_heads", [None]), ("migration_heads", "head-a"),
    ("event_count", True), ("event_count", 9.0), ("event_count", "9"),
    ("event_count", -1), ("event_count", None), ("event_head", False),
    ("event_head", 42.0), ("event_head", "42"), ("event_head", -1),
    ("event_head", None), ("event_hash", "A" * 64), ("event_hash", "a" * 63),
    ("event_hash", "sha256:" + "a" * 64), ("event_hash", None),
])
def test_baseline_rejects_malformed_values(tmp_path: Path, field: str, value: object) -> None:
    payload = _baseline_payload() | {field: value}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(payload))
    # Match malformed identity to prove validation does not rely on mismatch alone.
    identity = {field: value} if field in {
        "target_backup_label", "target_time", "account_id", "environment", "projector_version"
    } else {}
    with pytest.raises(EvidenceError):
        _load_baseline(path, **identity)


@pytest.mark.parametrize(("field", "value"), [
    ("target_backup_label", "20260903031700-F"), ("target_time", "2026-09-04T04:00:00Z"),
    ("account_id", "3f19d046-5030-494c-9a0a-9573bb890c2f"),
    ("environment", "shadow"), ("projector_version", "v4"),
])
def test_baseline_requires_exact_request_identity(tmp_path: Path, field: str, value: str) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(_baseline_payload()))
    with pytest.raises(EvidenceError):
        _load_baseline(path, **{field: value})


@pytest.mark.parametrize("kind", ["relative", "large", "directory", "symlink", "fifo", "missing", "extra", "missing-field", "duplicate", "invalid-json", "invalid-utf8"])
def test_baseline_requires_bounded_regular_json(tmp_path: Path, kind: str) -> None:
    import os

    path = tmp_path / "baseline.json"
    payload = _baseline_payload()
    if kind == "relative":
        path = Path("baseline.json")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text(json.dumps(payload))
        path.symlink_to(target)
    elif kind == "invalid-utf8":
        path.write_bytes(b"\xff")
    elif kind != "missing":
        if kind == "extra":
            payload["raw"] = TOKEN_SENTINEL
        elif kind == "missing-field":
            del payload["target_time"]
        content = json.dumps(payload)
        if kind == "large":
            content += " " * 65536
        elif kind == "duplicate":
            content = content[:-1] + ', "event_count": 9}'
        elif kind == "invalid-json":
            content = "[]"
        path.write_text(content)
    with pytest.raises(EvidenceError):
        _load_baseline(path)


def test_baseline_accepts_exact_size_and_empty_event_stream(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    content = json.dumps(_baseline_payload() | {"event_count": 0, "event_head": None})
    path.write_text(content + " " * (65536 - len(content)))
    baseline = _load_baseline(path)
    assert baseline.event_count == 0
    assert baseline.event_head is None
    assert baseline.migration_heads == ("head-a", "head-b")


@pytest.mark.parametrize(("field", "value"), [
    ("migration_heads", ("head-a",)), ("event_count", 10), ("event_head", 43),
    ("event_hash", "b" * 64), ("account_id", "3f19d046-5030-494c-9a0a-9573bb890c2f"),
    ("environment", "shadow"), ("projector_version", "v4"),
])
def test_evidence_rejects_baseline_state_mismatch(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(EvidenceError):
        _render_baseline(tmp_path, baseline=replace(_baseline(), **{field: value}))


def _render_baseline(tmp_path: Path, **overrides: object):
    kwargs = {
        "schema_tsv": "180000\thead-a,head-b\t9", "replay_json": _replay_report(),
        "baseline": _baseline(), "elapsed_seconds": 37, "observed_at_ms": 1756961300000,
        "config_path": _config(tmp_path), "image_digest": f"sha256:{'b' * 64}",
        "network_name": "bfx-dr-20260904t031700z-a1b2c3d4e5f60718", "network_internal": True,
        "egress_disconnected": True, "image_labels": IMAGE_LABELS, "now_ms": 1756961300000,
    }
    return render_restore_evidence(**(kwargs | overrides))


@pytest.mark.parametrize(("field", "value"), [
    ("event_head", "42"), ("event_head", 42.0),
    ("replayed_count", 1.0), ("replayed_count", True),
])
def test_baseline_replay_rejects_noninteger_observations(tmp_path: Path, field: str, value: object) -> None:
    replay = json.loads(_replay_report())
    if field == "event_head":
        replay[field] = value
    else:
        replay["diagnostic_diff"]["offer_claims"][field] = value
    with pytest.raises(EvidenceError):
        _render_baseline(tmp_path, replay_json=json.dumps(replay))


def test_baseline_restore_cli_requires_matching_state(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    schema = tmp_path / "schema.tsv"
    replay = tmp_path / "replay.json"
    output = tmp_path / "restore.json"
    baseline.write_text(json.dumps(_baseline_payload()))
    schema.write_text("180000\thead-a,head-b\t9")
    replay.write_text(_replay_report())
    argv = (
        sys.executable, str(EVIDENCE_PATH), "restore", "--baseline", str(baseline),
        "--schema-tsv", str(schema), "--replay-json", str(replay),
        "--target-backup-label", "20260904031700-F", "--account-id",
        "3f19d046-5030-494c-9a0a-9573bb890c1f", "--environment", "prod",
        "--projector-version", "projector-v3", "--elapsed-seconds", "37",
        "--observed-at-ms", str(time.time_ns() // 1_000_000), "--egress-disconnected",
        "--image-labels", json.dumps(IMAGE_LABELS),
        "--config", str(_config(tmp_path)), "--image-digest", f"sha256:{'b' * 64}",
        "--network-name", "bfx-dr-20260904t031700z-a1b2c3d4e5f60718",
        "--network-internal", "--output", str(output),
    )
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert json.loads(output.read_text())["measured"] is True
    baseline.write_text(json.dumps(_baseline_payload() | {"event_head": 43}))
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert completed.returncode == 2
    assert json.loads(output.read_text())["measured"] is False
    assert TOKEN_SENTINEL not in output.read_text() + completed.stdout + completed.stderr


@pytest.mark.parametrize(("field", "value"), [
    ("observed_at_ms", True), ("observed_at_ms", 1.0), ("observed_at_ms", "1"),
    ("observed_at_ms", -1), ("egress_disconnected", False), ("egress_disconnected", 1),
])
def test_baseline_evidence_rejects_unvalidated_metadata(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(EvidenceError):
        _render_baseline(tmp_path, **{field: value})


def test_baseline_evidence_emits_validated_target_and_observation(tmp_path: Path) -> None:
    result = _render_baseline(tmp_path, baseline=replace(_baseline(), target_time="2026-09-04T04:00:00Z"))
    assert result["target_time"] == "2026-09-04T04:00:00Z"
    assert result["observed_at_ms"] == 1756961300000
    assert result["egress_disconnected"] is True


@pytest.mark.parametrize("age_ms", [-1, 900001])
def test_restore_freshness_rejects_future_and_stale_evidence(tmp_path: Path, age_ms: int) -> None:
    with pytest.raises(EvidenceError, match="restore_output_invalid"):
        _render_baseline(tmp_path, now_ms=1756961300000 + age_ms)


@pytest.mark.parametrize("age_ms", [0, 900000])
def test_restore_freshness_accepts_window_boundaries(tmp_path: Path, age_ms: int) -> None:
    report = _render_baseline(tmp_path, now_ms=1756961300000 + age_ms, image_labels=IMAGE_LABELS)
    assert report["measured"] is True
    assert report["image_labels"] == IMAGE_LABELS


@pytest.mark.parametrize("labels", [None, {}, [], {**IMAGE_LABELS, "org.bfx.pgbackrest.version": "2.60.0"}])
def test_renderer_image_labels_fail_closed(tmp_path: Path, labels: object) -> None:
    with pytest.raises(EvidenceError, match="restore_output_invalid"):
        _render_baseline(tmp_path, image_labels=labels)


def test_renderer_image_labels_drop_unbounded_extra_metadata(tmp_path: Path) -> None:
    report = _render_baseline(tmp_path, image_labels={**IMAGE_LABELS, "raw": TOKEN_SENTINEL})
    assert report["image_labels"] == IMAGE_LABELS
    assert TOKEN_SENTINEL not in json.dumps(report)


@pytest.mark.parametrize("value", [True, 1.0, -1, "1756875000", {"epoch": 1756875000}])
@pytest.mark.parametrize("field", ["start", "stop"])
def test_pinned_info_rejects_noninteger_epoch(field: str, value: object) -> None:
    payload = json.loads(_info_json())
    payload[0]["backup"][0]["timestamp"][field] = value
    with pytest.raises(EvidenceError, match="pgbackrest_info_invalid"):
        evidence._parse_pgbackrest_info(json.dumps(payload))


def test_pinned_info_rejects_stop_before_start() -> None:
    payload = json.loads(_info_json())
    payload[0]["backup"][0]["timestamp"] = {"start": 20, "stop": 19}
    with pytest.raises(EvidenceError, match="pgbackrest_info_invalid"):
        evidence._parse_pgbackrest_info(json.dumps(payload))


@pytest.mark.parametrize("kind", ["backup", "backup-invalid", "backup-failure"])
@pytest.mark.parametrize("fault", ["write", "replace", "write-and-unlink"])
def test_backup_enospc_revokes_green_at_halt2_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, fault: str,
) -> None:
    from scripts.halt2_cutover import _read_dr_measurement

    output = tmp_path / "backup.json"
    output.write_text(json.dumps({
        "measured": True, "rpo_seconds": 1, "observed_at_ms": time.time_ns() // 1_000_000,
    }))
    assert _read_dr_measurement(output, key="rpo_seconds") == 1
    archiver, info = tmp_path / "archiver.tsv", tmp_path / "info.json"
    archiver.write_text("1756961300000\t1756961240000\t00000001000000000000000A\t0\t")
    info.write_text(_info_json() if kind == "backup" else "{}")
    config = _config(tmp_path)
    def enospc(*args, **kwargs):
        raise OSError(errno.ENOSPC, TOKEN_SENTINEL)
    monkeypatch.setattr(evidence.os if fault == "replace" else evidence.tempfile,
                        "replace" if fault == "replace" else "NamedTemporaryFile", enospc)
    if fault == "write-and-unlink":
        unlink = Path.unlink
        def denied(path, *args, **kwargs):
            if path == output:
                raise PermissionError(TOKEN_SENTINEL)
            return unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", denied)
    argv = ["backup-failure", "--error-code", "pgbackrest_check_failed"] if kind == "backup-failure" else [
        "backup", "--archiver-tsv", str(archiver), "--info-json", str(info), "--config", str(config),
    ]
    assert evidence.main([*argv, "--output", str(output)]) == 2
    with pytest.raises(ValueError):
        _read_dr_measurement(output, key="rpo_seconds")


def _info_json() -> str:
    return json.dumps(
        [
            {
                "name": "bfx",
                "status": {"code": 0, "message": "ok"},
                "repo": [{"key": 1, "cipher": "aes-256-cbc", "status": {"code": 0, "message": "ok"}}],
                "backup": [
                    {
                        "label": "20260903031700-F",
                        "type": "full",
                        "timestamp": {
                            "start": 1756875000,
                            "stop": 1756875120,
                        },
                    },
                    {
                        "label": "20260904031700-D",
                        "type": "diff",
                        "timestamp": {
                            "start": 1756961220,
                            "stop": 1756961240,
                        },
                    },
                ],
            }
        ]
    )


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "pgbackrest.conf"
    config.write_text("[global]\nrepo1-type=s3\n", encoding="utf-8")
    return config


def test_backup_evidence_calculates_rpo_and_is_bounded(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = render_backup_evidence(
        archiver_tsv="1756961300000\t1756961240000\t00000001000000000000000A\t0\t",
        info_json=_info_json(),
        config_path=config,
        rpo_limit_seconds=300,
    )

    assert result == {
        "schema_version": 1,
        "measured": True,
        "rpo_seconds": 60,
        "observed_at_ms": 1756961300000,
        "stanza": "bfx",
        "repository": "r2",
        "last_archived_wal": "00000001000000000000000A",
        "latest_backup_label": "20260904031700-D",
        "config_digest": hashlib.sha256(config.read_bytes()).hexdigest(),
        "failed_archive_count": 0,
    }
    assert "repo1-s3-key-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    ("archiver_tsv", "info_json", "error_code"),
    [
        ("1756961300000\t\t\t0\t", _info_json(), "archive_never_confirmed"),
        (
            "1756961300000\t1756960999000\twal\t0\t",
            _info_json(),
            "archive_lag_exceeded",
        ),
        ("1756961300000\t1756961240000\twal\t0\t", "[]", "backup_set_missing"),
        ("not-json\trow", _info_json(), "archiver_output_invalid"),
    ],
)
def test_backup_evidence_fails_closed(
    tmp_path: Path,
    archiver_tsv: str,
    info_json: str,
    error_code: str,
) -> None:
    with pytest.raises(EvidenceError, match=f"^{error_code}$"):
        render_backup_evidence(
            archiver_tsv=archiver_tsv,
            info_json=info_json,
            config_path=_config(tmp_path),
            rpo_limit_seconds=300,
        )


def test_backup_evidence_accepts_exact_rpo_limit(tmp_path: Path) -> None:
    result = render_backup_evidence(
        archiver_tsv="1756961300000\t1756961000000\twal\t0\t",
        info_json=_info_json(),
        config_path=_config(tmp_path),
        rpo_limit_seconds=300,
    )

    assert result["measured"] is True
    assert result["rpo_seconds"] == 300


def test_archive_failure_is_fatal_only_when_newer_than_success(tmp_path: Path) -> None:
    config = _config(tmp_path)
    accepted = render_backup_evidence(
        archiver_tsv="1756961300000\t1756961240000\twal\t7\t1756961239000",
        info_json=_info_json(),
        config_path=config,
        rpo_limit_seconds=300,
    )
    assert accepted["failed_archive_count"] == 7

    with pytest.raises(EvidenceError, match=r"^archive_lag_exceeded$"):
        render_backup_evidence(
            archiver_tsv="1756961300000\t1756961240000\twal\t7\t1756961240001",
            info_json=_info_json(),
            config_path=config,
            rpo_limit_seconds=300,
        )


def test_backup_evidence_requires_a_full_backup(tmp_path: Path) -> None:
    info = json.loads(_info_json())
    info[0]["backup"] = [info[0]["backup"][1]]

    with pytest.raises(EvidenceError, match=r"^backup_set_missing$"):
        render_backup_evidence(
            archiver_tsv="1756961300000\t1756961240000\twal\t0\t",
            info_json=json.dumps(info),
            config_path=_config(tmp_path),
            rpo_limit_seconds=300,
        )


def test_backup_evidence_rejects_json_float_epoch(tmp_path: Path) -> None:
    info = json.loads(_info_json())
    info[0]["backup"][0]["timestamp"]["start"] = 1.0

    with pytest.raises(EvidenceError, match=r"^pgbackrest_info_invalid$"):
        render_backup_evidence(
            archiver_tsv="1756961300000\t1756961240000\twal\t0\t",
            info_json=json.dumps(info),
            config_path=_config(tmp_path),
            rpo_limit_seconds=300,
        )


def _replay_report(*, matches: bool = True) -> str:
    row_counts = {"event_log": 9}
    content_hashes = {"event_log": "a" * 64}
    diagnostic_diff: dict[str, dict[str, int | str | bool]] = {}
    for index, name in enumerate(PROJECTION_NAMES, start=1):
        digest = f"{index:x}" * 64
        row_counts[name] = index
        content_hashes[name] = digest
        diagnostic_diff[name] = {
            "old_count": index,
            "replayed_count": index,
            "old_hash": digest,
            "replayed_hash": digest,
            "matches": matches if name == PROJECTION_NAMES[0] else True,
        }
    return json.dumps(
        {
            "account_id": "3f19d046-5030-494c-9a0a-9573bb890c1f",
            "environment": "prod",
            "projector_version": "projector-v3",
            "event_head": 42,
            "event_hash": "a" * 64,
            "row_counts": row_counts,
            "content_hashes": content_hashes,
            "diagnostic_diff": diagnostic_diff,
            "raw": TOKEN_SENTINEL,
        }
    )


def test_restore_evidence_keeps_only_stable_bounded_fields(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = render_restore_evidence(
        schema_tsv="180000\thead-a,head-b\t9",
        replay_json=_replay_report(),
        baseline=_baseline(),
        observed_at_ms=1756961300000,
        now_ms=1756961300000,
        image_labels=IMAGE_LABELS,
        egress_disconnected=True,
        elapsed_seconds=37,
        config_path=config,
        image_digest=f"sha256:{'b' * 64}",
        network_name="bfx-dr-20260904t031700z-a1b2c3d4e5f60718",
        network_internal=True,
    )

    assert result["measured"] is True
    assert result["rto_seconds"] == 37
    assert result["event_hash"] == "a" * 64
    assert result["event_count"] == 9
    assert result["row_counts"]["offer_claims"] == 1
    assert result["projection_hashes"]["offer_claims"] == "1" * 64
    assert result["network_internal"] is True
    assert result["network_name"].startswith("bfx-dr-")
    assert result["config_digest"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert result["image_digest"] == f"sha256:{'b' * 64}"
    assert TOKEN_SENTINEL not in json.dumps(result)


def test_restore_evidence_rejects_projection_mismatch(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError, match=r"^projection_replay_mismatch$"):
        render_restore_evidence(
            schema_tsv="180000\thead-a\t9",
            replay_json=_replay_report(matches=False),
            baseline=_baseline(),
            observed_at_ms=1756961300000,
            now_ms=1756961300000,
            image_labels=IMAGE_LABELS,
            egress_disconnected=True,
            elapsed_seconds=37,
            config_path=_config(tmp_path),
            image_digest=f"sha256:{'b' * 64}",
            network_name="bfx-dr-20260904t031700z-a1b2c3d4e5f60718",
            network_internal=True,
        )


def test_failure_evidence_rejects_unbounded_error_codes() -> None:
    result = render_failure_evidence(
        kind="backup", error_code="archiver_output_invalid", observed_at_ms=1234
    )
    assert result == {
        "schema_version": 1,
        "measured": False,
        "kind": "backup",
        "observed_at_ms": 1234,
        "error_code": "archiver_output_invalid",
    }
    with pytest.raises(EvidenceError):
        render_failure_evidence(kind="backup", error_code=TOKEN_SENTINEL, observed_at_ms=1234)
    assert TOKEN_SENTINEL not in json.dumps(result)


def test_backup_cli_writes_atomic_bounded_failure(tmp_path: Path) -> None:
    archiver = tmp_path / "archiver.tsv"
    info = tmp_path / "info.json"
    output = tmp_path / "backup.json"
    archiver.write_text(TOKEN_SENTINEL, encoding="utf-8")
    info.write_text(_info_json(), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(EVIDENCE_PATH),
            "backup",
            "--archiver-tsv",
            str(archiver),
            "--info-json",
            str(info),
            "--config",
            str(_config(tmp_path)),
            "--output",
            str(output),
            "--rpo-limit-seconds",
            "300",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert TOKEN_SENTINEL not in completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["measured"] is False
    assert report["error_code"] == "archiver_output_invalid"
    assert TOKEN_SENTINEL not in json.dumps(report)
    assert not list(tmp_path.glob(".backup.json.*.tmp"))
