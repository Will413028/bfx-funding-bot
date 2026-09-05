"""Offline contracts for bounded pgBackRest DR evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_PATH = ROOT / "deploy/vm/pgbackrest/evidence.py"
TOKEN_SENTINEL = "TOKEN-SENTINEL"
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


def _info_json() -> str:
    return json.dumps(
        [
            {
                "name": "bfx",
                "backup": [
                    {
                        "label": "20260903031700-F",
                        "type": "full",
                        "timestamp": {
                            "start": {"epoch": 1756875000},
                            "stop": {"epoch": 1756875120},
                        },
                    },
                    {
                        "label": "20260904031700-D",
                        "type": "diff",
                        "timestamp": {
                            "start": {"epoch": 1756961220},
                            "stop": {"epoch": 1756961240},
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
    info[0]["backup"][0]["timestamp"]["start"]["epoch"] = 1.0

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
        target_backup_label="20260904031700-F",
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
            target_backup_label="20260904031700-F",
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
