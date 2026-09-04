"""Path E subprocess harness — invoked by test_path_e_subprocess.py.

Wraps daemon.build_daemon to (1) swap executor.submit with one that raises
ExecutorAuthError and (2) wrap daemon.run() so a synthetic task inside the
daemon's TaskGroup calls submit() once at startup. Then runs daemon.main()
unmodified — _run's `except* ExecutorAuthError → sys.exit(78)`
handler must catch the propagated error and exit the process with 78.

Not a pytest file (leading underscore) — pytest skips it during collection.
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from typing import Any
from uuid import uuid4

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.marketfeed import daemon as daemon_mod
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
)

_orig_build_daemon = daemon_mod.build_daemon


async def _patched_build_daemon(*args: Any, **kwargs: Any) -> Any:
    kwargs.setdefault("skip_ws", True)
    d = await _orig_build_daemon(*args, **kwargs)

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
