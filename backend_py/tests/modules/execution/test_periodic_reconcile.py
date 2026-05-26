import asyncio
from dataclasses import dataclass

import pytest

from bfx_funding_bot.modules.execution.boot_recovery import ReconcileResult
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.marketfeed.schemas import HealthStatus, HealthTarget


class _FakeProbe:
    def __init__(self):
        self.beats: list[str] = []
        self.updates: list[tuple] = []

    def record_heartbeat(self, sub_task: str) -> None:
        self.beats.append(sub_task)

    def update(self, target, status, **fields) -> None:
        self.updates.append((target, status, fields))


@dataclass
class _FakeRecovery:
    results: list  # each item: ReconcileResult OR an Exception to raise
    _i: int = 0

    async def run(self) -> ReconcileResult:
        item = self.results[min(self._i, len(self.results) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_loop_runs_reconcile_each_interval_and_heartbeats():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 0, 0)])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.01, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.035)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert recovery._i >= 2
    assert "periodic_reconcile" in probe.beats


@pytest.mark.asyncio
async def test_divergence_on_periodic_release_sets_degraded():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ReconcileResult(0, 2, 0)])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.01, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.02)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert any(
        t == HealthTarget.RECONCILE and s == HealthStatus.DEGRADED
        for (t, s, _f) in probe.updates
    )


@pytest.mark.asyncio
async def test_divergence_clears_on_clean_tick():
    """RECONCILE goes DEGRADED on drift tick, then HEALTHY on the next clean tick."""
    probe = _FakeProbe()
    # tick 1: drift (n_claimed=2), tick 2+: clean (all zeros)
    recovery = _FakeRecovery(results=[ReconcileResult(0, 2, 0), ReconcileResult(0, 0, 0)])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.005, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.04)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    reconcile_updates = [(s) for (t, s, _f) in probe.updates if t == HealthTarget.RECONCILE]
    assert reconcile_updates, "expected at least one RECONCILE update"
    assert reconcile_updates[-1] == HealthStatus.HEALTHY, (
        f"expected last RECONCILE update to be HEALTHY, got {reconcile_updates}"
    )


@pytest.mark.asyncio
async def test_consecutive_fetch_failures_trip_executor_down_failsafe():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[RuntimeError("venue unreachable")])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.005, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    assert any(
        t == HealthTarget.EXECUTOR and s == HealthStatus.DOWN
        for (t, s, _f) in probe.updates
    )


@pytest.mark.asyncio
async def test_recovery_after_failure_clears_failsafe():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[
        RuntimeError("x"), RuntimeError("x"), RuntimeError("x"), ReconcileResult(0, 0, 0),
    ])
    pr = PeriodicReconcile(
        recovery=recovery, probe=probe, interval_s=0.005, max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.06)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_soon())
    exec_updates = [(s) for (t, s, _f) in probe.updates if t == HealthTarget.EXECUTOR]
    assert exec_updates and exec_updates[-1] == HealthStatus.HEALTHY
