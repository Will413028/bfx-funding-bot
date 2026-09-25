"""Where the daemon and the planner raise automatic protections."""
from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bfx_funding_bot.modules.execution.capital_repository import CapitalBlockedError
from bfx_funding_bot.modules.execution.safety.protection import (
    AutomaticProtection,
    WriterLockWatch,
)


class Recorder:
    def __init__(self) -> None:
        self.trips: list[tuple[str, str]] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.trips.append((trigger, detail))


class _RaisingCapital:
    """A capital runtime whose read meets an unclassifiable commitment."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.session_factory = self._session

    @contextlib.asynccontextmanager
    async def _session(self):  # type: ignore[no-untyped-def]
        yield None

    async def read(self, **kwargs):  # type: ignore[no-untyped-def]
        raise CapitalBlockedError(self.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "expected"), [
    ("unclassifiable_commitment", ["unclassifiable_commitment"]),
    ("snapshot_query_pending", []),  # a transient observation state blocks, never halts
])
async def test_planner_capital_read_trips_only_on_protection_reasons(reason, expected) -> None:
    from tests.modules.execution.deployment.test_reconciler import _build, _post_quote
    recorder = Recorder()
    rec, executor, *_ = _build(exposure=Decimal("0"), quotes=[_post_quote("fUST_a30")],
                               capital_runtime=_RaisingCapital(reason))
    rec._protection = recorder
    await rec.deploy()
    assert [trigger for trigger, _ in recorder.trips] == expected
    assert executor.ready_submissions == []


class _Lock:
    def __init__(self, results: list[bool]) -> None:
        self.results = results

    async def refresh(self) -> bool:
        return self.results.pop(0)


async def test_liveness_loop_trips_when_the_writer_lock_is_lost(monkeypatch) -> None:
    from bfx_funding_bot.modules.marketfeed import daemon as daemon_module

    rounds = 0

    async def one_round(awaitable, timeout):  # type: ignore[no-untyped-def]
        # First wait times out (do the check), the second returns (stop requested).
        nonlocal rounds
        rounds += 1
        awaitable.close()
        if rounds == 1:
            raise TimeoutError
        return None

    monkeypatch.setattr(daemon_module.asyncio, "wait_for", one_round)
    recorder = Recorder()
    lock = _Lock([False])
    heartbeats: list[str] = []
    fake = SimpleNamespace(
        _stop_event=asyncio.Event(), writer_lock=lock,
        writer_lock_watch=WriterLockWatch(lock=lock, protection=recorder),
        probe=SimpleNamespace(record_heartbeat=heartbeats.append),
    )
    await daemon_module.Daemon._writer_lock_liveness_loop(fake)  # type: ignore[arg-type]
    assert rounds == 2
    assert [trigger for trigger, _ in recorder.trips] == ["writer_lock_lost"]
    assert heartbeats == []


async def test_a_refused_boot_observation_makes_its_trips_durable_before_exit() -> None:
    from bfx_funding_bot.modules.marketfeed.daemon import Daemon

    protection = AutomaticProtection()
    engaged: list[str] = []

    class _Kill:
        async def engage(self, *, cause, actor, reason):  # type: ignore[no-untyped-def]
            engaged.append(actor)
            from bfx_funding_bot.modules.execution.safety.kill_switch import KillResult
            from bfx_funding_bot.modules.execution.safety.trading_state import TradingState
            return KillResult(state=TradingState(1, "HALTED", cause, actor, reason, 0),
                              state_changed=True, cancel_all=())

    protection.bind(_Kill())

    class _Recovery:
        async def run(self):  # type: ignore[no-untyped-def]
            protection.trip("unattributed_offer", "orphan at boot")
            raise CapitalBlockedError("unattributed_offer")

    fake = SimpleNamespace(boot_recovery=_Recovery(), protection=protection)
    with pytest.raises(CapitalBlockedError):
        await Daemon._run_boot_recovery(fake)  # type: ignore[arg-type]
    assert engaged == ["auto:unattributed_offer"]
