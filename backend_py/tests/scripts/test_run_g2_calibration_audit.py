from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.run_g2_calibration_audit import (
    build_report,
    compute_m1,
    compute_m2,
    compute_m3,
    parse_log_line,
    render_markdown,
)


def event(event_type: str, correlation_id: str | None = "c1", **extra: object) -> dict:
    value: dict[str, object] = {
        "timestamp": "2026-08-30T00:00:00+00:00",
        "event_type": event_type,
        "correlation_id": correlation_id,
        "cell": "fUSD_p30",
        "strategy": "mean_reversion",
        "payload": {},
    }
    value.update(extra)
    return value


def test_parser_reads_json_after_python_logging_prefix_and_ignores_plain_text() -> None:
    payload = {"event_type": "signal", "correlation_id": "c1"}
    assert parse_log_line(f"2026-08-30 00:00:00 INFO bfx {json.dumps(payload)}") == payload
    assert parse_log_line("not structured telemetry") is None
    assert parse_log_line('{"not": "an event"}') == {"not": "an event"}


def test_m1_separates_direction_match_from_strict_state_match_and_tracks_quality() -> None:
    signals = [
        event("signal", payload={"signal_direction": "post"}),
        event("signal", correlation_id="c2", cell="fUST_p30", payload={"signal_direction": "skip"}),
        event("signal", correlation_id=None),
    ]
    divergences = [
        event(
            "signal_divergence",
            payload={
                "divergence_detail": {
                    "live": {"signal_direction": "post"},
                    "replay": {"signal_direction": "post"},
                }
            },
        ),
        event(
            "signal_divergence",
            correlation_id="c2",
            cell="fUST_p30",
            payload={
                "divergence_detail": {
                    "live": {"signal_direction": "skip"},
                    "replay": {"signal_direction": "post"},
                }
            },
        ),
        event("signal_divergence", correlation_id="orphan"),
    ]
    result = compute_m1(signals + divergences + [signals[0], divergences[0]])

    assert result["total"] == 2
    assert result["signal_matching"] == 1
    assert result["state_matching"] == 0
    assert result["signal_match_rate"] == 0.5
    assert result["state_match_rate"] == 0.0
    assert result["data_quality"] == {
        "direction_unknown": 0,
        "orphan_divergence": 1,
        "malformed_events": 1,
    }
    assert {row["cell"] for row in result["per_cell"]} == {"fUSD_p30", "fUST_p30"}


def test_m1_reports_unknown_direction_and_deduplicates_malformed_keys() -> None:
    result = compute_m1(
        [
            event("signal", payload={"signal_direction": "post"}),
            event("signal_divergence", payload={"divergence_detail": {"live": {}, "replay": {}}}),
            event("signal", correlation_id="", payload={"signal_direction": "post"}),
            event("signal", payload={"signal_direction": "post"}),
        ]
    )
    assert result["total"] == 1
    assert result["signal_matching"] == 0
    assert result["state_matching"] == 0
    assert result["data_quality"]["direction_unknown"] == 1
    assert result["data_quality"]["malformed_events"] == 1


def test_m2_reports_adjacent_numeric_and_categorical_drift() -> None:
    result = compute_m2(
        {
            "results": [
                {
                    "cell": "fUSD_p30",
                    "strategy": "MeanReversionStrategy",
                    "windows": [
                        {"test_start_mts": 1, "best_params": {"span": 10, "mode": "a"}},
                        {"test_start_mts": 2, "best_params": {"span": 20, "mode": "b"}},
                    ],
                }
            ]
        }
    )
    assert result["status"] == "available"
    assert result["rows"][0]["drift"] == 1.0
    assert {field["parameter"] for field in result["rows"][0]["fields"]} == {"span", "mode"}


def test_m2_is_unavailable_without_two_valid_windows() -> None:
    assert compute_m2(None)["status"] == "unavailable"
    assert (
        compute_m2(
            {"results": [{"cell": "fUSD_p30", "strategy": "mean_reversion", "windows": []}]}
        )["status"]
        == "unavailable"
    )


def test_m3_uses_strictly_greater_than_threshold_and_counts_unknown() -> None:
    events = [
        event(
            "health_check",
            payload={
                "check_target": "signal_pipeline",
                "reason": "stale_exceeded",
                "stale_seconds": 300,
            },
        ),
        event(
            "health_check",
            payload={
                "check_target": "signal_pipeline",
                "reason": "stale_exceeded",
                "stale_seconds": 301,
            },
        ),
        event(
            "health_check",
            payload={
                "check_target": "signal_pipeline",
                "reason": "stale_exceeded",
                "stale_seconds": "unknown",
            },
        ),
    ]
    assert compute_m3(events) == {
        "total_stale_events": 3,
        "gaps_over_threshold": 1,
        "max_known_gap_seconds": 301.0,
        "unknown_gap_count": 1,
    }


def test_report_and_markdown_are_metrics_only_with_unset_thresholds() -> None:
    report = build_report([json.dumps(event("signal"))])
    assert report["mode"] == "metrics_only"
    assert report["decision"] == "not_evaluated"
    assert report["thresholds"] is None
    markdown = render_markdown(report)
    assert "metrics_only" in markdown
    assert "not_evaluated" in markdown
    assert "PASS" not in markdown and "FAIL" not in markdown


def test_cli_reads_stdin_and_writes_markdown_and_sibling_json(tmp_path: Path) -> None:
    output = tmp_path / "audit.md"
    completed = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "scripts.run_g2_calibration_audit",
            "--input",
            "-",
            "--output",
            str(output),
        ],
        input=json.dumps(event("signal")) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert output.exists() and output.with_suffix(".json").exists()
    assert json.loads(output.with_suffix(".json").read_text())["decision"] == "not_evaluated"
