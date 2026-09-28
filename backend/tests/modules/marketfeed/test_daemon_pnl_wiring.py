"""build_daemon wires the NAV-drop alert to a real NAV source, and nothing blocks on it.

Lending envelope D3: a NAV drop is level 4 -- an alert, never a stop. The
ReconcileNavTracker behind it is subscribed to PositionReconciled, so a venue
snapshot showing a NAV drop alerts once while every guard still allows a POST.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.events import PositionReconciled
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from tests.modules.marketfeed.account_test_helpers import (
    configure_account_env,
    seed_exchange_account,
)


def _write_cells_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "cells.yaml"
    # fUST: funded canary currency (caps {fUSD: 0, fUST: 3000}); canary boot now
    # asserts cap > 0 per configured symbol (assert_caps_invariant).
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUST
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    return yaml_path


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


def _post() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    symbol="fUST")


@pytest.mark.asyncio
async def test_a_nav_drop_alerts_and_never_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    # The live guard set, plus the explicit caps a simulated phase sizes from.
    safety_live = tmp_path / "safety.yaml"
    safety_live.write_text(
        (Path(__file__).parents[3] / "configs" / "safety.live.yaml").read_text().replace(
            "allocation_cap: {enabled: false}",
            "allocation_cap: {enabled: true, caps: {fUST: 3000}, default_cap: 0}"))
    # Pure NAV wiring test: no legacy canary phase or financial release authority.
    monkeypatch.setenv("BFX_PHASE", "shadow")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "ci")
    monkeypatch.setenv("BFX_EXECUTION_POLICY", "book_guarded")
    monkeypatch.setenv("BFX_BOOK_MAX_AGE_SECONDS", "30")
    monkeypatch.setenv("BFX_BOOK_RECONCILE_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("BFX_BOOK_MAX_DOWN_PCT", "0.15")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_live))
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "pnl_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    configure_account_env(monkeypatch)
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)  # paper executor is fine
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base, make_async_engine_from_url
    _eng = make_async_engine_from_url(f"sqlite+aiosqlite:///{db_path}")
    async with _eng.begin() as _c:
        await _c.run_sync(Base.metadata.create_all)
    await seed_exchange_account(_eng, capital_policies=False)
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.apps.bot import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    from bfx_funding_bot.modules.execution.safety import protection as protection_module
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(protection_module.alerts, "emit",
                        lambda event, **fields: sent.append((event, fields)))
    names = {guard.name for guard in daemon.safety_chain.guards}
    assert not names & {"realized_loss_24h", "drawdown_from_peak", "divergence_rate"}

    ctx = AccountContext(
        "550e8400-e29b-41d4-a716-446655440000", Credentials("k", "s"), Decimal("500")
    )
    # 100 -> 40 on fUST: a 60% 24h loss (> 5%) and drawdown (> 10%).
    await daemon.bus.publish(_reconciled("100", 1_000))
    await daemon.bus.publish(_reconciled("40", 2_000))
    assert sorted(fields["metric"] for event, fields in sent if event == "nav_drop") == [
        "drawdown_pct", "realized_loss_pct_24h"]
    for guard in daemon.safety_chain.guards:
        if guard.name in {"manual_kill", "heartbeat", "auth_health", "allocation_cap",
                          "buying_power"}:
            continue
        assert (await guard.evaluate(_post(), ctx)).allowed is True, guard.name
