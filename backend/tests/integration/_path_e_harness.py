"""Path E subprocess harness — invoked by test_path_e_subprocess.py.

Wraps daemon.build_daemon to (1) swap executor.submit with one that raises
ExecutorAuthError and (2) wrap daemon.run() so a synthetic task inside the
daemon's TaskGroup calls submit() once at startup. Then runs daemon.main()
unmodified — _run's `except* ExecutorAuthError → sys.exit(78)`
handler must catch the propagated error and exit the process with 78.

The daemon is the Bitfinex-venue composition; nothing here may reach the network, so the
candle warm-up is skipped and the book/auth WebSocket tasks and the venue observation loops
are detached (the same set ``go_offline`` detaches in the sqlite wiring tests).

Not a pytest file (leading underscore) — pytest skips it during collection.
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from typing import Any
from uuid import uuid4

from bfx_funding_bot.apps import bot as daemon_mod
from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

_orig_build_daemon = daemon_mod.build_daemon


async def _no_warmup(**_kw: Any) -> None:
    return None


daemon_mod.warmup_cell = _no_warmup  # type: ignore[assignment]


def _detach_network(d: Any) -> None:
    async def idle(stop_event: Any) -> None:
        await stop_event.wait()

    if d.funding_book_service is not None:
        d.funding_book_service.run = idle
    if d.ws_dispatcher is not None:
        d.ws_dispatcher.run = idle
    d.boot_recovery = None
    d.periodic_reconcile = None
    d.interest_ledger_sync = None
    d.credit_history_sync = None


async def _patched_build_daemon(*args: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("skip_ws", True)
    d = await _orig_build_daemon(*args, **kwargs)
    _detach_network(d)

    async def _auth_fail_submit(*_a: Any, **_kw: Any) -> Any:
        raise ExecutorAuthError("subprocess_synthetic_auth_fail")

    d.executor.submit = _auth_fail_submit  # type: ignore[method-assign]

    _orig_run = d.run
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    symbol="fUST")
    ctx = AccountContext(
        "550e8400-e29b-41d4-a716-446655440000",
        Credentials("k", "s"),
        Decimal("500"),
    )

    async def _wrapped_run() -> None:
        async def _force_submit() -> None:
            # Brief delay so daemon.run's TaskGroup spins up its sub-tasks
            # before we trigger the auth failure.
            await asyncio.sleep(0.5)
            await d.executor.submit(decision, ctx)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_orig_run(), name="daemon_run_inner")
            tg.create_task(_force_submit(), name="force_submit")

    d.run = _wrapped_run  # type: ignore[method-assign]
    return d


daemon_mod.build_daemon = _patched_build_daemon  # type: ignore[assignment]


if __name__ == "__main__":
    daemon_mod.main()
    # _run's `except* ExecutorAuthError` calls sys.exit(78). If we reach
    # here, the handler didn't fire — surface a distinct code so the test
    # assertion message points at the wiring, not the exit-code constant.
    sys.exit(99)
