"""A bot run's lifecycle in ``bot_runs``, and the next boot as its observer.

Some exits leave no trace in the process: the loop watchdog's ``_exit``, an OOM kill,
SIGKILL, a segfault, a host crash. Industry practice has the supervisor report restarts
(Kubernetes restart counts to Alertmanager, systemd ``OnFailure=``); here the
supervisor is Docker without a socket the bot may read, so the next boot is the
observer. ``start`` runs once the process holds the writer lock and has been built: in
one transaction it closes every open run of the scope as ``unclean`` and inserts its own
row, then alerts once per run it closed. ``finish`` records how this run ended, after
the drain, on the paths that can still write (see ``apps.bot._run_daemon``); a run that
never reaches it stays open for the next boot to report.

Best effort, never in the way of trading: a failed ``start`` or ``finish`` is logged and
the boot or the exit carries on. A failed ``finish`` makes the next boot report the run
as unclean, an extra alert rather than a missing one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.observability.tables import END_REASONS, BotRunRow

log = logging.getLogger(__name__)

UNCLEAN = "unclean"
FINISH_TIMEOUT_S = 5.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class UncleanRun:
    run_id: UUID
    started_at_ms: int
    source_revision: str | None
    host_name: str


class BotRunRecord:
    """This process's row in ``bot_runs``."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        environ: Mapping[str, str] = os.environ,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._session_factory = session_factory
        self.exchange_account_id = exchange_account_id
        self.deployment_environment = deployment_environment
        self.run_id = uuid4()
        self._environ = environ
        self._clock_ms = clock_ms
        self.started = False

    async def start(self) -> list[UncleanRun]:
        """Close the scope's open runs as unclean, insert this run, alert per closed run.

        Returns the runs closed (empty when the previous run ended cleanly)."""
        try:
            closed = await self._start()
        except Exception:
            log.exception("bot_run_start_failed run_id=%s", self.run_id)
            return []
        self.started = True
        noticed_at_ms = self._clock_ms()
        for run in closed:
            log.error("previous_bot_run_unclean run_id=%s started_at=%s host=%s",
                      run.run_id, _iso(run.started_at_ms), run.host_name)
            alerts.emit(
                alerts.PREVIOUS_RUN_UNCLEAN, level=alerts.CRITICAL,
                run_id=str(run.run_id), started_at=_iso(run.started_at_ms),
                noticed_at=_iso(noticed_at_ms), source_revision=run.source_revision or "unknown",
                host=run.host_name,
                hint="likely the loop watchdog, an OOM kill or a SIGKILL; see the container "
                     "logs between started_at and noticed_at",
            )
        return closed

    async def _start(self) -> list[UncleanRun]:
        now = self._clock_ms()
        async with self._session_factory.begin() as session:
            open_runs = (await session.scalars(
                select(BotRunRow).where(
                    BotRunRow.exchange_account_id == self.exchange_account_id,
                    BotRunRow.deployment_environment == self.deployment_environment,
                    BotRunRow.end_reason.is_(None),
                ).order_by(BotRunRow.started_at_ms).with_for_update()
            )).all()
            closed = [UncleanRun(row.run_id, row.started_at_ms, row.source_revision, row.host_name)
                      for row in open_runs]
            if open_runs:
                await session.execute(
                    update(BotRunRow)
                    .where(BotRunRow.run_id.in_([row.run_id for row in open_runs]))
                    .values(end_reason=UNCLEAN, end_recorded_at_ms=now)
                )
            session.add(BotRunRow(
                run_id=self.run_id,
                exchange_account_id=self.exchange_account_id,
                deployment_environment=self.deployment_environment,
                started_at_ms=now,
                source_revision=self._environ.get("BFX_SOURCE_REVISION") or None,
                image_digest=self._environ.get("BFX_IMAGE_DIGEST") or None,
                host_name=socket.gethostname(),
                pid=os.getpid(),
            ))
        return closed

    async def finish(self, reason: str, *, timeout_s: float = FINISH_TIMEOUT_S) -> None:
        """Record how this run ended; bounded, so a dead database cannot hold the exit."""
        if reason not in END_REASONS or reason == UNCLEAN:
            raise ValueError(f"a run cannot end itself as {reason!r}")
        if not self.started:
            return
        try:
            async with asyncio.timeout(timeout_s):
                async with self._session_factory.begin() as session:
                    await session.execute(
                        update(BotRunRow)
                        .where(BotRunRow.run_id == self.run_id, BotRunRow.end_reason.is_(None))
                        .values(end_reason=reason, end_recorded_at_ms=self._clock_ms())
                    )
        except Exception:
            log.exception("bot_run_finish_failed run_id=%s reason=%s "
                          "(the next boot will report this run as unclean)", self.run_id, reason)
            return
        log.info("bot_run_finished run_id=%s reason=%s", self.run_id, reason)
