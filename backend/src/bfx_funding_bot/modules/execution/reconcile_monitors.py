"""Operator alerts the ledger's venue reconcile raises.

``ForeignExposureMonitor`` reports venue offers no durable intent traces to;
``QuarantineAgeMonitor`` reports UNKNOWN submits that have quarantined their
symbol for too long. Both keep their dedup state across reconcile cycles.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.modules.observability import alerts


class ForeignExposureMonitor:
    """Alert once per foreign venue offer id (D2, ladder level 4).

    Shared by the boot and the runtime reconcile so a restart of neither
    repeats an alert the process already sent. Ids that left the book are
    forgotten, which keeps the set as small as the account's live offers.
    """

    def __init__(self) -> None:
        self._alerted: set[str] = set()

    def observe(self, foreign: list[ActiveFundingOffer], *, active_ids: set[str]) -> None:
        self._alerted &= active_ids
        for offer in foreign:
            if offer.venue_offer_id in self._alerted:
                continue
            self._alerted.add(offer.venue_offer_id)
            alerts.emit(alerts.FOREIGN_EXPOSURE, venue_offer_id=offer.venue_offer_id,
                        symbol=offer.symbol, amount=offer.amount,
                        amount_original=offer.amount_original, rate=offer.rate,
                        period_days=offer.period_days, mts_created=offer.mts_created)


QUARANTINE_ALERT_AFTER_MS = 30 * 60 * 1000
QUARANTINE_REPEAT_MS = 6 * 60 * 60 * 1000


class AgingUnknown(Protocol):
    """What the age monitor needs of an open UNKNOWN, whichever authority recorded it.

    ``attempt_id`` is the uncertainty's own id (an UNKNOWN submit's attempt id).
    """

    @property
    def attempt_id(self) -> UUID: ...
    @property
    def symbol(self) -> str: ...
    @property
    def started_at_ms(self) -> int: ...
    @property
    def amount(self) -> Decimal: ...


class QuarantineAgeMonitor:
    """Alert when an UNKNOWN has held its symbol for too long (D3 level 2).

    The quarantine itself never escalates to a halt -- it already stops new
    offers for that symbol -- but lending there is paused until evidence or an
    operator resolves it, so after ``QUARANTINE_ALERT_AFTER_MS`` the operator
    is told, and reminded every ``QUARANTINE_REPEAT_MS`` while it lasts.
    """

    def __init__(self, *, alert_after_ms: int = QUARANTINE_ALERT_AFTER_MS,
                 repeat_ms: int = QUARANTINE_REPEAT_MS) -> None:
        self._after = alert_after_ms
        self._repeat = repeat_ms
        self._last: dict[UUID, int] = {}

    def observe(self, still_open: Sequence[AgingUnknown], *, now_ms: int) -> None:
        open_ids = {attempt.attempt_id for attempt in still_open}
        self._last = {key: at for key, at in self._last.items() if key in open_ids}
        for attempt in still_open:
            age = now_ms - attempt.started_at_ms
            last = self._last.get(attempt.attempt_id)
            if age < self._after or (last is not None and now_ms - last < self._repeat):
                continue
            self._last[attempt.attempt_id] = now_ms
            alerts.emit(alerts.UNKNOWN_QUARANTINE_AGED, symbol=attempt.symbol,
                        attempt_id=str(attempt.attempt_id), minutes=age // 60_000,
                        amount=str(attempt.amount))
