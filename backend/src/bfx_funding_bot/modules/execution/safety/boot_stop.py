"""A live boot that refuses to run: alert, then let the process exit.

A refused live boot (the database is at another schema than this build, or an
applied capital policy is unreadable) writes nothing and sends nothing to the
venue. Lending envelope ADR 2026-09-25 D3/D5: a refused process places nothing
by itself, and a durable HALTED would outlive the cause -- the next, fixed
build would still need an operator's resume, although a release is never a
trading decision. The managed offers already at the venue stay inside the
envelope they were placed under; a critical alert says so, and the operator's
kill (a venue cancel-all) is the way to pull them.

The caller then refuses the boot; the supervisor restarts the container.
"""
from __future__ import annotations

import logging
from typing import Final
from uuid import UUID

from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

VENUE_OFFERS_MAY_REMAIN: Final = "venue_offers_may_remain"


def report_refused_boot(*, account_id: UUID, environment: str, reason: str) -> None:
    """Tell the operator; never raises (the caller is already failing the boot)."""
    reason = reason[:500]
    log.critical("venue_offers_may_remain account=%s env=%s reason=%s", account_id,
                 environment, reason)
    alerts.emit(VENUE_OFFERS_MAY_REMAIN, level=alerts.CRITICAL,
                message="the bot refused to boot; its offers may still be at the venue "
                        "(the kill switch cancels them)",
                account=str(account_id), environment=environment, reason=reason)


__all__ = ["VENUE_OFFERS_MAY_REMAIN", "report_refused_boot"]
