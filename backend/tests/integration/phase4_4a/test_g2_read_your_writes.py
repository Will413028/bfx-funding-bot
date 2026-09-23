"""G2: bus.publish 返回後所有 subscribers 已 observe event."""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.events import ReservationClaimed

from .conftest import make_reservation_ref


@pytest.mark.integration
@pytest.mark.asyncio
async def test_after_publish_returns_ledger_already_updated(
    domain_chain: dict[str, Any],
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    registry = domain_chain["registry"]
    sig_id = uuid4()
    voi = "42"

    await bus.publish(ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        signal_correlation_id=sig_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000,
        symbol="fUST",
        reservation_ref=make_reservation_ref(42, sig_id, voi),
    ))

    # Immediately after publish returns — both ledger + registry reflect
    assert ledger.current_exposure("fUST") == Decimal("100")
    assert "42" in registry.snapshot()
