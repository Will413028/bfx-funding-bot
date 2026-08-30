from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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
                    "budget_hours": 12,
                    "windows": [
                        {"test_start_mts": 1, "budget_hours": 12, "best_params": {"span": 10, "mode": "a"}},
                        {"test_start_mts": 2, "budget_hours": 12, "best_params": {"span": 20, "mode": "b"}},
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
            {"results": [{"cell": "fUSD_p30", "strategy": "mean_reversion", "budget_hours": 12, "windows": []}]}
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


def test_report_handles_non_object_wfo_json_as_unavailable() -> None:
    report = build_report([], manifest=[])
    assert report["m2"]["status"] == "unavailable"
    assert report["m2"]["reason"] == "WFO manifest must be a JSON object"


def test_m2_preserves_class_name_strategy_label_in_output() -> None:
    result = compute_m2(
        {
            "results": [
                {
                    "cell": "fUSD_p30",
                    "strategy": "MeanReversionStrategy",
                    "budget_hours": 12,
                    "windows": [
                        {"test_start_mts": 1, "budget_hours": 12, "best_params": {"span": 10}},
                        {"test_start_mts": 2, "budget_hours": 12, "best_params": {"span": 20}},
                    ],
                }
            ]
        }
    )
    assert result["rows"][0]["strategy"] == "MeanReversionStrategy"
    assert result["strategies"] == ["MeanReversionStrategy"]


def test_m2_treats_serialized_decimal_strings_as_numeric_and_keeps_categories() -> None:
    result = compute_m2(
        {
            "results": [
                {
                    "cell": "fUSD_p30",
                    "strategy": "mean_reversion",
                    "budget_hours": 12,
                    "windows": [
                        {
                            "test_start_mts": 1,
                            "budget_hours": 12,
                            "best_params": {
                                "span": "1.0",
                                "mode": "a",
                                "leading_zero_label": "01",
                                "scientific": "1e-1",
                                "nonfinite": "NaN",
                                "flag": True,
                            },
                        },
                        {
                            "test_start_mts": 2,
                            "budget_hours": 12,
                            "best_params": {
                                "span": "1.2",
                                "mode": "b",
                                "leading_zero_label": "02",
                                "scientific": "2e-1",
                                "nonfinite": "Infinity",
                                "flag": False,
                            },
                        },
                    ],
                }
            ]
        }
    )

    fields = {field["parameter"]: field["drift"] for field in result["rows"][0]["fields"]}
    assert fields["span"] == pytest.approx(1 / 6)
    assert fields["scientific"] == pytest.approx(1 / 2)
    assert fields["mode"] == 1
    assert fields["leading_zero_label"] == 1
    assert fields["nonfinite"] == 1
    assert fields["flag"] == 1


def test_m2_keeps_different_budget_hours_in_separate_cohorts() -> None:
    result = compute_m2(
        {
            "results": [
                {
                    "cell": "fUSD_p30",
                    "strategy": "mean_reversion",
                    "budget_hours": 6,
                    "windows": [
                        {"test_start_mts": 1, "budget_hours": 6, "best_params": {"span": 1}},
                        {"test_start_mts": 3, "budget_hours": 6, "best_params": {"span": 2}},
                    ],
                },
                {
                    "cell": "fUSD_p30",
                    "strategy": "mean_reversion",
                    "budget_hours": 12,
                    "windows": [
                        {"test_start_mts": 2, "budget_hours": 12, "best_params": {"span": 10}},
                        {"test_start_mts": 4, "budget_hours": 12, "best_params": {"span": 20}},
                    ],
                },
            ]
        }
    )

    assert result["status"] == "available"
    assert {(row["budget_hours"], row["from"], row["to"]) for row in result["rows"]} == {
        (6, 1, 3),
        (12, 2, 4),
    }


@pytest.mark.parametrize(
    "manifest",
    [
        {
            "results": [
                {
                    "cell": "fUSD_p30",
                    "strategy": "mean_reversion",
                    "windows": [
                        {"test_start_mts": 1, "best_params": {"span": 1}},
                        {"test_start_mts": 2, "best_params": {"span": 2}},
                    ],
                }
            ]
        },
        {
            "results": [
                {
                    "cell": "fUSD_p30",
                    "strategy": "mean_reversion",
                    "budget_hours": 6,
                    "windows": [
                        {"test_start_mts": 1, "budget_hours": 6, "best_params": {"span": 1}},
                        {"test_start_mts": 2, "budget_hours": 12, "best_params": {"span": 2}},
                    ],
                }
            ]
        },
    ],
    ids=["missing-budget", "inconsistent-budget"],
)
def test_m2_rejects_missing_or_inconsistent_budget_hours(manifest: dict) -> None:
    result = compute_m2(manifest)

    assert result["status"] == "unavailable"
    assert "budget_hours" in result["reason"]


def test_markdown_includes_computed_evidence_and_unavailable_reason() -> None:
    report = build_report(
        [
            json.dumps(event("signal")),
            json.dumps(
                event(
                    "signal_divergence", payload={"divergence_detail": {"live": {}, "replay": {}}}
                )
            ),
            json.dumps(
                event(
                    "health_check",
                    payload={
                        "check_target": "signal_pipeline",
                        "reason": "stale_exceeded",
                        "stale_seconds": 301,
                    },
                )
            ),
        ]
    )
    markdown = render_markdown(report)
    for expected in (
        "divergent",
        "direction unknown",
        "fUSD_p30",
        "pairwise",
        "mean drift",
        "WFO window manifest not provided",
        "maximum known gap",
        "301.0",
    ):
        assert expected in markdown
