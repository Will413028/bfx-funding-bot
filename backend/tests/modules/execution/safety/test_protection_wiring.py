"""Where the daemon and the planner raise automatic protections."""
from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.safety.protection import (
    AutomaticProtection,
    WriterLockLostError,
    WriterLockWatch,
)
from bfx_funding_bot.modules.ledger import CapitalBlocked, Scope


class _BootRefusedError(RuntimeError):
    """A boot recovery's refusal (any exception type: the daemon re-raises it)."""


class Recorder:
    def __init__(self) -> None:
        self.trips: list[tuple[str, str]] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.trips.append((trigger, detail))


class _RefusingCapital:
    """A capital authority whose read meets a refusal (the port returns, never raises it)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    @contextlib.asynccontextmanager
    async def session_factory(self):  # type: ignore[no-untyped-def]
        yield None

    async def read(self, scope, *, now_ms, session=None):  # type: ignore[no-untyped-def]
        return CapitalBlocked(self.reason)


SCOPE = Scope(UUID(int=7), "test")


class _NoUncertainty:
    async def has_open(self, session, scope, symbol) -> bool:  # type: ignore[no-untyped-def]
        return False


def _refusing_ports(reason: str) -> dict[str, object]:
    """The planner's ports as apps builds them, over a refusing capital authority."""
    from tests.modules.execution.deployment.test_reconciler import _capital_ports
    capital = _RefusingCapital(reason)
    return _capital_ports(capital, offers=SimpleNamespace(), uncertainty=_NoUncertainty(),
                          scope=SCOPE, session_factory=capital.session_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "expected"), [
    ("unclassifiable_commitment", ["unclassifiable_commitment"]),
    ("snapshot_query_pending", []),  # a transient observation state blocks, never halts
    # Ledger codes (S1-2c): an integrity conflict halts; an unbounded tail retries.
    ("attempt_evidence_conflict", ["identity_conflict"]),
    ("attempt_tail_unbounded", []),
])
async def test_planner_capital_read_trips_only_on_protection_reasons(reason, expected) -> None:
    from tests.modules.execution.deployment.test_reconciler import _build, _post_quote
    recorder = Recorder()
    rec, executor, *_ = _build(exposure=Decimal("0"), quotes=[_post_quote("fUST_a30")],
                               capital_ports=_refusing_ports(reason))
    rec._protection = recorder
    await rec.deploy()
    assert [trigger for trigger, _ in recorder.trips] == expected
    assert executor.ready_submissions == []


class _Lock:
    def __init__(self, results: list[bool]) -> None:
        self.results = results

    async def refresh(self) -> bool:
        return self.results.pop(0)


async def test_liveness_loop_exits_when_the_writer_lock_is_lost(monkeypatch) -> None:
    """Process fencing, not a trading decision (lending envelope D3): the loop
    raises, the task group ends the daemon, and nothing is tripped or halted."""
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
    lock = _Lock([False])
    heartbeats: list[str] = []
    fake = SimpleNamespace(
        _stop_event=asyncio.Event(), writer_lock=lock,
        writer_lock_watch=WriterLockWatch(lock=lock),
        probe=SimpleNamespace(record_heartbeat=heartbeats.append),
    )
    with pytest.raises(WriterLockLostError):
        await daemon_module.Daemon._writer_lock_liveness_loop(fake)  # type: ignore[arg-type]
    assert rounds == 1
    assert heartbeats == []


async def test_a_refused_boot_observation_makes_its_trips_durable_before_exit() -> None:
    from bfx_funding_bot.modules.marketfeed.daemon import Daemon

    protection = AutomaticProtection()
    engaged: list[str] = []

    class _Trading:
        async def transition(self, state, *, cause, actor, reason, now_ms=None):  # type: ignore[no-untyped-def]
            engaged.append(actor)
            from bfx_funding_bot.modules.execution.safety.trading_state import (
                TradingState,
                TransitionResult,
            )
            return TransitionResult(state=TradingState(1, state, cause, actor, reason, 0),
                                    changed=True)

    protection.bind(_Trading())

    class _Recovery:
        async def run(self, scope):  # type: ignore[no-untyped-def]
            protection.trip("identity_conflict", "conflict at boot")
            raise _BootRefusedError("offer_provenance_conflict")

    fake = SimpleNamespace(boot_recovery=_Recovery(), protection=protection,
                           observation_scope=object())
    with pytest.raises(_BootRefusedError):
        await Daemon._run_boot_recovery(fake)  # type: ignore[arg-type]
    assert engaged == ["auto:identity_conflict"]
