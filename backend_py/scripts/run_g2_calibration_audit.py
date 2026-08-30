"""Offline, metrics-only G2 audit for structured canary stdout logs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any


def parse_log_line(line: str) -> dict[str, Any] | None:
    """Parse the first JSON object in a Docker/Python-prefixed log line."""
    start = line.find("{")
    if start < 0:
        return None
    try:
        value = json.loads(line[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _key(event: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    values = [event.get(name) for name in ("correlation_id", "cell", "strategy")]
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    event_type = event.get("event_type")
    if not isinstance(event_type, str):
        return None
    return (event_type, values[0], values[1], values[2])  # type: ignore[return-value]


def _direction(event: Mapping[str, Any]) -> tuple[str | None, str | None]:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None, None
    detail = payload.get("divergence_detail")
    if not isinstance(detail, Mapping):
        return None, None
    live = detail.get("live")
    replay = detail.get("replay")
    live_direction = live.get("signal_direction") if isinstance(live, Mapping) else None
    replay_direction = replay.get("signal_direction") if isinstance(replay, Mapping) else None
    return (
        live_direction if isinstance(live_direction, str) else None,
        replay_direction if isinstance(replay_direction, str) else None,
    )


def compute_m1(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    signals: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    divergences: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    quality = {"direction_unknown": 0, "orphan_divergence": 0, "malformed_events": 0}
    for event in events:
        event_type = event.get("event_type")
        if event_type not in ("signal", "signal_divergence"):
            continue
        key = _key(event)
        if key is None:
            quality["malformed_events"] += 1
            continue
        target = signals if event_type == "signal" else divergences
        target.setdefault(key, event)

    for key in divergences:
        if ("signal", *key[1:]) not in signals:
            quality["orphan_divergence"] += 1

    per_cell: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"cell": "", "total": 0, "signal_matching": 0, "state_matching": 0}
    )
    signal_matching = state_matching = divergent = direction_unknown = 0
    for key, _signal in signals.items():
        signal_key = ("signal", *key[1:])
        row = per_cell[signal_key[2]]
        row["cell"] = signal_key[2]
        row["total"] += 1
        divergence = divergences.get(("signal_divergence", *key[1:]))
        if divergence is None:
            signal_matching += 1
            state_matching += 1
            row["signal_matching"] += 1
            row["state_matching"] += 1
            continue
        divergent += 1
        live, replay = _direction(divergence)
        if live is None or replay is None:
            direction_unknown += 1
            quality["direction_unknown"] += 1
        elif live == replay:
            signal_matching += 1
            row["signal_matching"] += 1

    rows = []
    for row in sorted(per_cell.values(), key=lambda item: item["cell"]):
        total = row["total"]
        row["signal_match_rate"] = row["signal_matching"] / total if total else None
        row["state_match_rate"] = row["state_matching"] / total if total else None
        rows.append(row)
    total = len(signals)
    return {
        "total": total,
        "signal_matching": signal_matching,
        "state_matching": state_matching,
        "divergent": divergent,
        "direction_unknown": direction_unknown,
        "signal_match_rate": signal_matching / total if total else None,
        "state_match_rate": state_matching / total if total else None,
        "per_cell": rows,
        "data_quality": quality,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "rows": [],
        "max_drift": None,
        "mean_drift": None,
    }


def _strategy_label(value: Any) -> str | None:
    names = {
        "mean_reversion": "mean_reversion",
        "MeanReversionStrategy": "mean_reversion",
        "rate_percentile": "rate_percentile",
        "RatePercentileStrategy": "rate_percentile",
        "adaptive_period": "adaptive_period",
        "AdaptivePeriodStrategy": "adaptive_period",
    }
    return names.get(value) if isinstance(value, str) else None


def compute_m2(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    if manifest is None:
        return _unavailable("WFO window manifest not provided")
    results = manifest.get("results")
    if not isinstance(results, list):
        return _unavailable("manifest results is missing or malformed")
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for result in results:
        if not isinstance(result, Mapping):
            return _unavailable("manifest result row is malformed")
        cell, strategy, windows = (
            result.get("cell"),
            _strategy_label(result.get("strategy")),
            result.get("windows"),
        )
        if (
            not isinstance(cell, str)
            or not cell
            or strategy is None
            or not isinstance(windows, list)
        ):
            return _unavailable("manifest result row has malformed cell, strategy, or windows")
        for window in windows:
            if not isinstance(window, Mapping) or not isinstance(
                window.get("best_params"), Mapping
            ):
                return _unavailable("manifest window has malformed best_params")
            start = window.get("test_start_mts", window.get("window_idx"))
            if not isinstance(start, (int, float)):
                return _unavailable("manifest window lacks test_start_mts or window_idx")
            groups[(cell, strategy)].append({"start": start, "params": window["best_params"]})
    if not groups or any(len(windows) < 2 for windows in groups.values()):
        return _unavailable("fewer than two WFO windows for a cell and strategy")
    rows: list[dict[str, Any]] = []
    for (cell, strategy), windows in sorted(groups.items()):
        ordered = sorted(windows, key=lambda item: item["start"])
        for old, new in pairwise(ordered):
            shared = set(old["params"]) & set(new["params"])
            drifts = []
            for field in sorted(shared):
                before, after = old["params"][field], new["params"][field]
                if (
                    isinstance(before, (int, float))
                    and isinstance(after, (int, float))
                    and not isinstance(before, bool)
                    and not isinstance(after, bool)
                ):
                    drift = abs(after - before) / max(abs(before), abs(after), 1e-12)
                else:
                    drift = 1 if before != after else 0
                drifts.append({"parameter": field, "drift": drift})
            rows.append(
                {
                    "cell": cell,
                    "strategy": strategy,
                    "from": old["start"],
                    "to": new["start"],
                    "fields": drifts,
                    "drift": max((x["drift"] for x in drifts), default=0),
                }
            )
    values = [row["drift"] for row in rows]
    return {
        "status": "available",
        "rows": rows,
        "max_drift": max(values),
        "mean_drift": sum(values) / len(values),
    }


def compute_m3(
    events: Iterable[Mapping[str, Any]], gap_threshold_seconds: int = 300
) -> dict[str, Any]:
    stale = gaps = unknown = 0
    known: list[float] = []
    for event in events:
        if event.get("event_type") != "health_check":
            continue
        payload = event.get("payload")
        if (
            not isinstance(payload, Mapping)
            or payload.get("check_target") != "signal_pipeline"
            or payload.get("reason") != "stale_exceeded"
        ):
            continue
        stale += 1
        value = payload.get("stale_seconds")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            unknown += 1
        else:
            known.append(float(value))
            if value > gap_threshold_seconds:
                gaps += 1
    return {
        "total_stale_events": stale,
        "gaps_over_threshold": gaps,
        "max_known_gap_seconds": max(known, default=None),
        "unknown_gap_count": unknown,
    }


def _timestamp_bounds(events: list[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    values = [e["timestamp"] for e in events if isinstance(e.get("timestamp"), str)]
    return (min(values), max(values)) if values else (None, None)


def build_report(
    lines: Iterable[str],
    manifest: Mapping[str, Any] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    events: list[Mapping[str, Any]] = [
        event for line in lines if (event := parse_log_line(line)) is not None
    ]
    start, end = _timestamp_bounds(events)
    m1, m2, m3 = compute_m1(events), compute_m2(manifest), compute_m3(events)
    quality = dict(m1.pop("data_quality"))
    return {
        "schema_version": 1,
        "observation_window": {"start": window_start or start, "end": window_end or end},
        "mode": "metrics_only",
        "decision": "not_evaluated",
        "thresholds": None,
        "m1": m1,
        "m2": m2,
        "m3": m3,
        "data_quality": quality,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    m1, m2, m3 = report["m1"], report["m2"], report["m3"]
    lines = [
        "# G2 Calibration Audit",
        "",
        f"- mode: `{report['mode']}`",
        f"- decision: `{report['decision']}`",
        "- thresholds: unset",
        "",
        "## M1 — Signal parity",
        "",
        f"- signals: {m1['total']}",
        f"- signal match rate: {m1['signal_match_rate']}",
        f"- state match rate: {m1['state_match_rate']}",
        "",
        "## M2 — WFO parameter drift",
        "",
        f"- status: {m2['status']}",
        f"- max drift: {m2['max_drift']}",
        "",
        "## M3 — Feed gaps",
        "",
        f"- stale events: {m3['total_stale_events']}",
        f"- gaps over threshold: {m3['gaps_over_threshold']}",
        f"- unknown gaps: {m3['unknown_gap_count']}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--wfo-json")
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    args = parser.parse_args(argv)
    lines = (
        sys.stdin
        if args.input == "-"
        else Path(args.input).read_text(encoding="utf-8").splitlines()
    )
    manifest = (
        json.loads(Path(args.wfo_json).read_text(encoding="utf-8")) if args.wfo_json else None
    )
    report = build_report(lines, manifest, args.window_start, args.window_end)
    output = Path(args.output)
    output.write_text(render_markdown(report), encoding="utf-8")
    output.with_suffix(".json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
