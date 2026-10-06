"""Offline contracts for bounded pgBackRest DR evidence."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from tests.scripts.dr_measurement import read_measurement

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_PATH = ROOT / "deploy/vm/pgbackrest/evidence.py"
TOKEN_SENTINEL = "TOKEN-SENTINEL"
IMAGE_LABELS = {
    "org.bfx.postgresql.base-digest": "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2",
    "org.bfx.pgbackrest.version": "2.59.1",
    "org.bfx.pgbackrest.source-sha256": "1cd522afc33b8ff846ef88c55dc238717c9c8817a4f6ca7c9f64887de9c7402d",
}


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
@pytest.mark.parametrize("script", ["evidence.py", "restore_drill.py"])
def test_host_entrypoint_remains_standalone_stdlib(script):
    result = subprocess.run((sys.executable, "-I", "-S", str(EVIDENCE_PATH.with_name(script)), "--help"),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


ACCOUNT = "3f19d046-5030-494c-9a0a-9573bb890c1f"
NOW_MS = 1756961300000


def _bounds():
    ledger = evidence._ledger
    lines = ["schema\t180000\thead-a", *(f"table\t{name}" for name in ledger.LEDGER_TABLES),
             f"scope\t{ACCOUNT}\tprod\t5\t5\t4\t3\t1\t7", "epoch\t2\tledger",
             "inconsistent\tclock_behind\t0", "inconsistent\tvenue_offer_mirror_orphans\t0",
             "inconsistent\tvenue_credit_mirror_orphans\t0"]
    return ledger.parse_bounds("\n".join(lines) + "\n")


def _render_ledger(tmp_path: Path, **overrides: object):
    kwargs = {
        "kind": "restore_ledger", "bounds": _bounds(), "ledger": {"rows_compared": 9},
        "boot": {"authority": "ledger"}, "target_backup_label": "20260904031700-F",
        "elapsed_seconds": 37, "observed_at_ms": NOW_MS, "config_path": _config(tmp_path),
        "image_digest": f"sha256:{'b' * 64}",
        "network_name": "bfx-dr-20260904t031700z-a1b2c3d4e5f60718", "network_internal": True,
        "egress_disconnected": True, "image_labels": IMAGE_LABELS,
        "verifier_image_digest": f"sha256:{'c' * 64}", "now_ms": NOW_MS,
    }
    return evidence.render_ledger_restore_evidence(**(kwargs | overrides))


@pytest.mark.parametrize(("field", "value"), [
    ("observed_at_ms", True), ("observed_at_ms", 1.0), ("observed_at_ms", "1"),
    ("observed_at_ms", -1), ("egress_disconnected", False), ("egress_disconnected", 1),
    ("verifier_image_digest", "sha256:short"), ("target_backup_label", "bad;label"),
    ("elapsed_seconds", -1), ("network_internal", False), ("restore_test", 1),
])
def test_restore_evidence_rejects_unvalidated_metadata(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(EvidenceError):
        _render_ledger(tmp_path, **{field: value})


def test_restore_test_receipt_has_no_target_time(tmp_path: Path) -> None:
    result = _render_ledger(tmp_path)
    assert (result["restore_test"], result["target_time"]) == (True, None)
    with pytest.raises(EvidenceError, match="restore_output_invalid"):
        _render_ledger(tmp_path, target_time="2026-09-04T04:00:00Z")


def test_acceptance_receipt_carries_its_target(tmp_path: Path) -> None:
    result = _render_ledger(tmp_path, restore_test=False, target_time="2026-09-04T04:00:00Z")
    assert result["kind"] == "restore_ledger"
    assert (result["restore_test"], result["target_time"]) == (False, "2026-09-04T04:00:00Z")
    assert result["target_backup_label"] == "20260904031700-F"
    assert result["observed_at_ms"] == NOW_MS
    assert result["egress_disconnected"] is True
    assert result["config_digest"] == hashlib.sha256(_config(tmp_path).read_bytes()).hexdigest()
    no_target = _render_ledger(tmp_path, restore_test=False)
    assert (no_target["restore_test"], no_target["target_time"]) == (False, None)


@pytest.mark.parametrize("target_time", ["2026-09-04 04:00:00", "0", "2026-09-04T04:00:00", 1756961300])
def test_acceptance_receipt_rejects_a_malformed_target_time(tmp_path: Path, target_time: object) -> None:
    with pytest.raises(EvidenceError, match="restore_output_invalid"):
        _render_ledger(tmp_path, restore_test=False, target_time=target_time)


@pytest.mark.parametrize("age_ms", [-1, 900001])
def test_restore_freshness_rejects_future_and_stale_evidence(tmp_path: Path, age_ms: int) -> None:
    with pytest.raises(EvidenceError, match="restore_output_invalid"):
        _render_ledger(tmp_path, now_ms=NOW_MS + age_ms)


@pytest.mark.parametrize("age_ms", [0, 900000])
def test_restore_freshness_accepts_window_boundaries(tmp_path: Path, age_ms: int) -> None:
    report = _render_ledger(tmp_path, now_ms=NOW_MS + age_ms, image_labels=IMAGE_LABELS)
    assert report["measured"] is True
    assert report["image_labels"] == IMAGE_LABELS


@pytest.mark.parametrize("labels", [None, {}, [], {**IMAGE_LABELS, "org.bfx.pgbackrest.version": "2.60.0"}])
def test_renderer_image_labels_fail_closed(tmp_path: Path, labels: object) -> None:
    with pytest.raises(EvidenceError, match="restore_output_invalid"):
        _render_ledger(tmp_path, image_labels=labels)


def test_renderer_image_labels_drop_unbounded_extra_metadata(tmp_path: Path) -> None:
    report = _render_ledger(tmp_path, image_labels={**IMAGE_LABELS, "raw": TOKEN_SENTINEL})
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
def test_backup_enospc_revokes_green_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, fault: str,
) -> None:

    output = tmp_path / "backup.json"
    output.write_text(json.dumps({
        "schema_version": 1, "kind": "backup", "measured": True, "rpo_seconds": 1, "observed_at_ms": time.time_ns() // 1_000_000,
    }))
    output.chmod(0o600)
    assert read_measurement(output, key="rpo_seconds") == 1
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
        read_measurement(output, key="rpo_seconds")


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
        "kind": "backup",
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


@pytest.mark.parametrize("kind", ["restore", "archive_restore"])
def test_failure_evidence_refuses_the_retired_baseline_kinds(kind: str) -> None:
    with pytest.raises(EvidenceError):
        render_failure_evidence(kind=kind, error_code="restore_output_invalid", observed_at_ms=1)


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

