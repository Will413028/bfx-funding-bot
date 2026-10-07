"""Wait for a coroutine at most so long, without waiting for its cleanup.

``asyncio.timeout`` cancels the awaited work once and then waits for it to unwind; a
database that stopped answering without closing the socket (a frozen server) keeps that
unwinding -- a transaction's rollback, a connection close -- waiting just as long, so the
"bound" is not one. Here the work runs in its own task and the caller waits on the task
for at most ``timeout_s``; on expiry the task is cancelled and left behind. For use on
the way out of the process only: the abandoned task's connection is not returned
cleanly, which is fine because the process is exiting (``asyncio.run`` cancels what is
left and closes the loop).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable

log = logging.getLogger(__name__)


async def run_bounded(work: Awaitable[None], *, timeout_s: float, what: str) -> bool:
    """True when ``work`` finished within ``timeout_s``; its exception propagates.

    False when it did not: ``work`` is cancelled and abandoned, not awaited."""
    task = asyncio.ensure_future(work)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if not done:
        task.cancel()
        log.error("bounded_work_abandoned what=%s timeout_s=%s", what, timeout_s)
        return False
    task.result()
    return True
