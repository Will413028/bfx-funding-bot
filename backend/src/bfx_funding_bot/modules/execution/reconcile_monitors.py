"""Operator alerts the ledger's venue reconcile raises.

``ForeignExposureMonitor`` reports venue offers no durable intent traces to;
``QuarantineAgeMonitor`` reports UNKNOWN submits that have quarantined their
symbol for too long; ``StoppedOffersMonitor`` reports a stopped currency whose
managed offers are still at the venue. All keep their state across cycles.
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


STOPPED_OFFERS_ALERT_AFTER_MS = 30 * 60 * 1000
STOPPED_OFFERS_REPEAT_MS = 6 * 60 * 60 * 1000
_UNREADABLE = (alerts.STOP_STATE_UNREACHABLE, alerts.STOP_STATE_FAILED)


class StoppedOffersMonitor:
    """Alert when a currency that must hold no managed offer still does (D3 level 3, D4).

    The planner pulls a halted or disabled currency's managed offers every tick
    and normally they are gone within one or two. This reports the case where
    they are not -- the venue keeps refusing, the gate refuses for an open
    uncertainty, a provenance conflict, a bug -- whatever the cause, because it
    looks only at the state: a stop reason and the cancels that did not go
    through this tick (``venue_offer_ids``; None when the offers could not even
    be listed). An unreadable stop state is a condition of its own, with its own
    clock: the currency places nothing and nobody can tell whether it should be
    pulling. After 30 minutes, then every 6 hours.
    """

    def __init__(self, *, alert_after_ms: int = STOPPED_OFFERS_ALERT_AFTER_MS,
                 repeat_ms: int = STOPPED_OFFERS_REPEAT_MS) -> None:
        self._after = alert_after_ms
        self._repeat = repeat_ms
        # (symbol, "standing" | "unreadable") -> (since_ms, last_alert_ms)
        self._held: dict[tuple[str, str], tuple[int, int | None]] = {}

    def observe(self, symbol: str, *, reason: str | None,
                venue_offer_ids: Sequence[str] | None, now_ms: int) -> None:
        if reason in _UNREADABLE:
            # Whether offers stand is unknown this tick: the standing clock keeps running.
            self._due_then_emit((symbol, "unreadable"), symbol, reason, None, now_ms)
            return
        self._held.pop((symbol, "unreadable"), None)
        if reason is None or (venue_offer_ids is not None and not venue_offer_ids):
            self._held.pop((symbol, "standing"), None)
            return
        self._due_then_emit((symbol, "standing"), symbol, reason, venue_offer_ids, now_ms)

    def _due_then_emit(self, key: tuple[str, str], symbol: str, reason: str,
                       venue_offer_ids: Sequence[str] | None, now_ms: int) -> None:
        since, last = self._held.get(key, (now_ms, None))
        due = now_ms - since >= self._after and (last is None or now_ms - last >= self._repeat)
        self._held[key] = (since, now_ms if due else last)
        if not due:
            return
        unreadable = key[1] == "unreadable"
        alerts.emit(
            alerts.STOPPED_OFFERS_STANDING, symbol=symbol, reason=reason,
            minutes=(now_ms - since) // 60_000,
            offers=("unknown" if venue_offer_ids is None or unreadable
                    else len(venue_offer_ids)),
            venue_offer_ids=",".join(venue_offer_ids or ()),
        )
