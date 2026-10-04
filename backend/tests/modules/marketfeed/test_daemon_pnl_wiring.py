"""build_daemon wires the NAV-drop alert to a real NAV source, and nothing blocks on it.

Lending envelope D3: a NAV drop is level 4 -- an alert, never a stop. The
ReconcileNavTracker behind it is subscribed to PositionReconciled, so a venue
snapshot showing a NAV drop alerts once while every guard still allows a POST.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.events import PositionReconciled
from tests.modules.marketfeed.account_test_helpers import boot_live_construction


def _reconciled(available: str, ts: int) -> PositionReconciled:
    return PositionReconciled(
        account_id="550e8400-e29b-41d4-a716-446655440000",
        reserved_usdt=Decimal("0"),
        realized_usdt=Decimal("0"),
        available_usdt=Decimal(available),
        n_offers=0,
        n_credits=0,
        occurred_at_ms=ts,
    symbol="fUST")


@pytest.mark.asyncio
async def test_a_nav_drop_alerts_and_never_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    daemon, engine = await boot_live_construction(
        monkeypatch, tmp_path, httpx_mock, name="pnl_wiring",
        extra_env={"BFX_SERVICE_VERSION": "test-sha"},
    )
    await engine.dispose()

    from bfx_funding_bot.modules.execution.safety import protection as protection_module
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(protection_module.alerts, "emit",
                        lambda event, **fields: sent.append((event, fields)))
    names = {guard.name for guard in daemon.safety_chain.guards}
    assert not names & {"realized_loss_24h", "drawdown_from_peak", "divergence_rate"}

    # 100 -> 40 on fUST: a 60% 24h loss (> 5%) and drawdown (> 10%).
    await daemon.bus.publish(_reconciled("100", 1_000))
    await daemon.bus.publish(_reconciled("40", 2_000))
    assert sorted(fields["metric"] for event, fields in sent if event == "nav_drop") == [
        "drawdown_pct", "realized_loss_pct_24h"]
    # An alert, never a stop (D3): no protection tripped and no guard is a NAV guard.
    assert daemon.protection is not None and daemon.protection.pending_reason() is None
