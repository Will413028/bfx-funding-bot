"""PeriodicReconcile's loop over the observation cycle: cadence, resync, fail-safe, deploy.

The non-accepted streak and the deploy input are in ``test_periodic_reconcile_streak.py``.
"""
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.ledger import CycleResult, Scope
from tests.async_wait import run_until, running, until, yield_loop

ACCEPTED = CycleResult("accepted")


class _Input:
    """The deployment input: hands the deployment ``offers`` for every accepted cycle."""

    def __init__(self, offers: tuple[ActiveFundingOffer, ...] = ()) -> None:
        self._offers = offers

    async def offers(self, cycle: CycleResult) -> tuple[ActiveFundingOffer, ...]:
        return self._offers


def _make_periodic(*, recovery, scope=None, **kwargs):
    if kwargs.get("deployment") is not None:
        kwargs.setdefault("deployment_input", _Input())
    return PeriodicReconcile(resync=ResyncChannel(), recovery=recovery,
                             scope=scope or Scope(uuid4(), "ci"), **kwargs)


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
    """An observation sink: each item is a ``CycleResult`` or an exception to raise."""

    results: list
    _i: int = 0

    async def run(self, scope: Scope) -> CycleResult:
        item = self.results[min(self._i, len(self.results) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


def _reconcile_statuses(probe, target):
    return [s for (t, s, _f) in probe.updates if t == target]


@pytest.mark.asyncio
async def test_loop_runs_reconcile_each_interval_and_heartbeats():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ACCEPTED])
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=0.01, max_consecutive_failures=3,
    )

    await run_until(
        pr.run_loop, lambda: recovery._i >= 2, what="two reconcile ticks",
    )
    assert recovery._i >= 2
    assert "periodic_reconcile" in probe.beats


@pytest.mark.asyncio
async def test_consecutive_fetch_failures_trip_executor_down_failsafe():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[RuntimeError("venue unreachable")])
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=0.005, max_consecutive_failures=3,
    )

    await run_until(
        pr.run_loop,
        lambda: HealthStatus.DOWN in _reconcile_statuses(probe, HealthTarget.EXECUTOR),
        what="EXECUTOR DOWN",
    )
    assert any(
        t == HealthTarget.EXECUTOR and s == HealthStatus.DOWN
        for (t, s, _f) in probe.updates
    )


@pytest.mark.asyncio
async def test_recovery_after_failure_clears_failsafe():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[
        RuntimeError("x"), RuntimeError("x"), RuntimeError("x"), ACCEPTED,
    ])
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=0.005, max_consecutive_failures=3,
    )

    await run_until(
        pr.run_loop,
        lambda: recovery._i >= 4
        and _reconcile_statuses(probe, HealthTarget.EXECUTOR)[-1:] == [HealthStatus.HEALTHY],
        what="EXECUTOR HEALTHY after three failures",
    )
    exec_updates = _reconcile_statuses(probe, HealthTarget.EXECUTOR)
    assert exec_updates and exec_updates[-1] == HealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_request_resync_wakes_loop_before_interval():
    """A resync request triggers an off-interval tick well before interval_s."""
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ACCEPTED])
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=3600.0,  # only a trigger can cause tick 2
        max_consecutive_failures=3, min_resync_interval_s=0.0,
    )

    async with running(pr.run_loop):
        await until(lambda: recovery._i >= 1, what="tick 1 at loop start")
        pr.resync.request("reconnect")
        await until(lambda: recovery._i >= 2, what="tick 2 from the resync")
    assert recovery._i >= 2  # tick 1 at loop start + tick 2 from the resync


@pytest.mark.asyncio
async def test_repeated_requests_dedup_into_bounded_ticks():
    """Many request_resync calls before a wake collapse into exactly one extra tick."""
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ACCEPTED])
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=3600.0,
        max_consecutive_failures=3, min_resync_interval_s=0.05,
    )

    async with running(pr.run_loop):
        await until(lambda: recovery._i >= 1, what="tick 1 at loop start")
        for _ in range(20):
            pr.resync.request("seq_gap")  # storm before the loop wakes
        await until(lambda: recovery._i >= 2, what="the debounced resync tick")
        # The resync flag is consumed and the next wake is an hour away, so any
        # extra tick would have to come from the storm: give it every chance.
        await yield_loop(200)
    # tick 1 (loop start) + exactly one debounced resync tick despite 20 requests
    assert recovery._i == 2


@pytest.mark.asyncio
async def test_stop_during_debounce_exits_promptly():
    """Stopping while a resync is in its debounce wait exits without hanging."""
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ACCEPTED])
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=3600.0,
        max_consecutive_failures=3, min_resync_interval_s=100.0,  # long debounce
    )

    async with running(pr.run_loop, timeout=10.0):  # exit must beat the 100s debounce
        await until(lambda: recovery._i >= 1, what="tick 1 at loop start")
        pr.resync.request("reconnect")  # enters a 100s debounce wait
        await yield_loop(50)  # let the loop reach the debounce wait
    # leaving the block sets stop and awaits the loop: it must break the debounce


@pytest.mark.asyncio
async def test_resync_requested_before_loop_start_is_honored():
    """A resync set synchronously before run_loop still produces an early tick."""
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ACCEPTED])
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=3600.0,
        max_consecutive_failures=3, min_resync_interval_s=0.0,
    )
    pr.resync.request("reconnect")  # before the loop is even running

    await run_until(pr.run_loop, lambda: recovery._i >= 2, what="the pre-set resync tick")
    assert recovery._i >= 2  # tick 1 (loop start) + the pre-set resync tick


class _FakeDeployment:
    def __init__(self) -> None:
        self.calls = 0

    async def deploy(self, *, venue_offers=()) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_deployment_called_after_clean_reconcile():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ACCEPTED])
    deployment = _FakeDeployment()
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=0.02,
        max_consecutive_failures=3, deployment=deployment,
    )

    await run_until(pr.run_loop, lambda: deployment.calls >= 1, what="a deploy call")
    assert deployment.calls >= 1


@pytest.mark.asyncio
async def test_deployment_not_called_on_reconcile_failure():
    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[RuntimeError("venue down")])
    deployment = _FakeDeployment()
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=0.005,
        max_consecutive_failures=3, deployment=deployment,
    )

    # Drive a known number of failing ticks instead of a time window.
    await run_until(pr.run_loop, lambda: recovery._i >= 3, what="three failing ticks")
    assert recovery._i >= 3
    assert deployment.calls == 0


@pytest.mark.asyncio
async def test_deployment_exception_does_not_crash_loop():
    class _BoomDeployment:
        def __init__(self) -> None:
            self.calls = 0

        async def deploy(self, *, venue_offers=()) -> None:
            self.calls += 1
            raise RuntimeError("deploy boom")

    probe = _FakeProbe()
    recovery = _FakeRecovery(results=[ACCEPTED])
    deployment = _BoomDeployment()
    pr = _make_periodic(
        recovery=recovery, probe=probe, interval_s=0.005,
        max_consecutive_failures=3, deployment=deployment,
    )

    # A second deploy call proves the loop kept ticking after the first exception.
    await run_until(pr.run_loop, lambda: deployment.calls >= 2, what="a second deploy call")
    assert deployment.calls >= 2


@pytest.mark.asyncio
async def test_deploy_receives_the_offers_of_the_deployment_input():
    offer = ActiveFundingOffer(
        venue_offer_id="42", symbol="fUST", amount=Decimal("200"), rate=0.001,
        period_days=2, mts_created=0, status="ACTIVE",
    )

    class _CapturingDeployment:
        def __init__(self) -> None:
            self.received: list[tuple] = []

        async def deploy(self, *, venue_offers=()) -> None:
            self.received.append(venue_offers)

    dep = _CapturingDeployment()
    pr = _make_periodic(
        recovery=_FakeRecovery([ACCEPTED]), probe=_FakeProbe(), interval_s=90,
        deployment=dep, deployment_input=_Input((offer,)),
    )
    await pr._tick()
    assert dep.received == [(offer,)]


@pytest.mark.asyncio
async def test_periodic_expected_refusal_is_not_transport_failure():
    from unittest.mock import AsyncMock

    recovery = AsyncMock()
    recovery.run.return_value = CycleResult("fenced")
    periodic = _make_periodic(
        recovery=recovery, scope=Scope(uuid4(), "ci"), probe=_FakeProbe(), interval_s=90,
    )
    await periodic._tick()
    assert periodic._consecutive_failures == 0
