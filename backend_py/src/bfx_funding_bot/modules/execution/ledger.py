"""PaperPositionLedger — event-sourcing replay from Axiom.

NOT persisted separately. Axiom is SoT (30 day retention). On daemon
startup, fetches last N days of order_fill events and rebuilds in-memory
exposure. Listens to order_fill via on_order_fill() for live updates.

Axiom unreachable at boot → LedgerReplayError → daemon never starts.
Empty-ledger fallback is NOT acceptable — it would let AllocationCap
permit over-cap exposure (real safety hole).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from bfx_funding_bot.core.errors import LedgerReplayError
from bfx_funding_bot.modules.marketfeed.schemas import (
    EventType,
    OrderFillPayload,
)

log = logging.getLogger(__name__)

_REPLAY_DEFAULT_RETRIES = 3
_REPLAY_BACKOFF_BASE_S = 1.0


class _AxiomQueryProtocol(Protocol):
    async def query_order_fills(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]: ...


class PaperPositionLedger:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._exposure = Decimal("0")

    @classmethod
    async def replay_from_axiom(
        cls,
        *,
        account_id: str,
        since: datetime,
        axiom_query: _AxiomQueryProtocol,
        max_retries: int = _REPLAY_DEFAULT_RETRIES,
    ) -> PaperPositionLedger:
        last_exc: BaseException | None = None
        for attempt in range(1, max_retries + 1):
            try:
                events = await axiom_query.query_order_fills(account_id, since)
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    backoff = _REPLAY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                    log.warning(
                        "ledger_replay_attempt_failed attempt=%d/%d backoff=%.1fs err=%r",
                        attempt, max_retries, backoff, exc,
                    )
                    await asyncio.sleep(backoff)
                continue
            break
        else:
            raise LedgerReplayError(
                f"axiom replay exhausted {max_retries} attempts: {last_exc!r}"
            )

        ledger = cls(account_id=account_id)
        for ev in events:
            if ev.get("event_type") != EventType.ORDER_FILL.value:
                continue
            if ev.get("account_id") != account_id:
                continue
            try:
                payload = OrderFillPayload.model_validate(ev["payload"])
            except Exception:
                log.warning("ledger_replay_skip_malformed payload=%r", ev.get("payload"))
                continue
            ledger._apply_fill(payload)
        log.info(
            "ledger_replay_complete account=%s events=%d exposure=%s",
            account_id, len(events), ledger._exposure,
        )
        return ledger

    def on_order_fill(self, payload: OrderFillPayload) -> None:
        """Live listener: subscribed by daemon after replay."""
        self._apply_fill(payload)

    def _apply_fill(self, payload: OrderFillPayload) -> None:
        self._exposure += Decimal(str(payload.fill_size_usdt))

    def current_exposure(self) -> Decimal:
        return self._exposure
