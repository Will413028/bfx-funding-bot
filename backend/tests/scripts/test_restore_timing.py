"""Contracts for bounded restore-stage timing diagnostics."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
TIMING_PATH = ROOT / "deploy/vm/pgbackrest/restore_timing.py"


def _load_timing_module() -> ModuleType | None:
    if not TIMING_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("offsite_dr_restore_timing", TIMING_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


restore_timing = _load_timing_module()


def _timing(clock: Callable[[], float]):
    assert restore_timing is not None, "restore_timing.py is missing"
    return restore_timing.StageTiming(clock=clock)


def _clock(samples: list[float]) -> Callable[[], float]:
    values: Iterator[float] = iter(samples)
    return lambda: next(values)


def test_measures_elapsed_without_using_wall_clock() -> None:
    timing = _timing(_clock([10.0, 12.5]))

    timing.begin("verification")
    timing.end("verification")

    assert timing.render() == {"verification": 2.5}


@pytest.mark.parametrize("operation", ["begin", "end"])
def test_rejects_unknown_stage_names(operation: str) -> None:
    timing = _timing(_clock([10.0]))

    with pytest.raises(ValueError, match="stage_unknown"):
        getattr(timing, operation)("restore --db-url=secret")


def test_rejects_duplicate_completed_stage() -> None:
    timing = _timing(_clock([10.0, 11.0]))
    timing.begin("resource_setup")
    timing.end("resource_setup")

    with pytest.raises(ValueError, match="stage_duplicate"):
        timing.begin("resource_setup")


def test_rejects_end_without_matching_begin() -> None:
    timing = _timing(_clock([10.0]))

    with pytest.raises(ValueError, match="stage_not_active"):
        timing.end("cleanup")


def test_render_rejects_unclosed_stage() -> None:
    timing = _timing(_clock([10.0]))
    timing.begin("physical_and_wal_recovery")

    with pytest.raises(ValueError, match="stage_unclosed"):
        timing.render()


@pytest.mark.parametrize("sample", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_clock_values(sample: float) -> None:
    timing = _timing(_clock([sample]))

    with pytest.raises(ValueError, match="clock_invalid"):
        timing.begin("resource_setup")


def test_clock_failure_does_not_leave_an_active_stage() -> None:
    samples = iter((RuntimeError("clock unavailable"), 10.0, 11.0))

    def clock() -> float:
        sample = next(samples)
        if isinstance(sample, Exception):
            raise sample
        return sample

    timing = _timing(clock)

    with pytest.raises(ValueError, match="clock_invalid"):
        timing.begin("verification")

    timing.begin("verification")
    timing.end("verification")

    assert timing.render() == {"verification": 1.0}


def test_rejects_backwards_time() -> None:
    timing = _timing(_clock([12.5, 10.0]))
    timing.begin("verification")

    with pytest.raises(ValueError, match="clock_backwards"):
        timing.end("verification")


def test_rejects_overlapping_stages() -> None:
    timing = _timing(_clock([10.0]))
    timing.begin("resource_setup")

    with pytest.raises(ValueError, match="stage_overlap"):
        timing.begin("verification")
