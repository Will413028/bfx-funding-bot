"""build_daemon wires the canary L2 loss-limiter guards to a real NAV source.

Regression for the #4 safety gap: the canary invariant forces realized_loss_24h
+ drawdown_from_peak ON, but they were wired to _StubPnLSource (0/0) so they
could never trip. After the fix both guards share a ReconcileNavTracker that is
subscribed to PositionReconciled, so a venue snapshot showing a NAV drop makes
them block POST.
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
from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
    DrawdownGuard,
    RealizedLossGuard,
)
from bfx_funding_bot.modules.execution.safety.chain import GuardRule
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import (
    ReconcileNavTracker,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
)


def _write_cells_yaml(tmp_path: Path) -> Path:
    yaml_path = tmp_path / "cells.yaml"
    yaml_path.write_text("""
cells:
  - strategy: rate_percentile
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {percentile: 75, lookback_hours: 5}
    reference_amount_usdt: 150.0
phase3b_wfo_results_ref: x
""")
    return yaml_path


def _find_guard(guards: list[GuardRule], guard_cls: type) -> object:
    for g in guards:
        if isinstance(g, guard_cls):
            return g
    raise AssertionError(f"{guard_cls.__name__} not wired into the safety chain")


def _reconciled(available: str, ts: int) -> PositionReconciled:
    return PositionReconciled(
        account_id="default",
        reserved_usdt=Decimal("0"),
        realized_usdt=Decimal("0"),
        available_usdt=Decimal(available),
        n_offers=0,
        n_credits=0,
        occurred_at_ms=ts,
    )


def _post() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    )


@pytest.mark.asyncio
async def test_canary_loss_guards_use_nav_tracker_and_trip_on_drawdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    safety_canary = Path(__file__).parents[3] / "configs" / "safety.canary.yaml"
    monkeypatch.setenv("BFX_PHASE", "canary")
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_canary))
    monkeypatch.setenv("BFX_SERVICE_VERSION", "test-sha")
    db_path = tmp_path / "pnl_wiring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
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
    await _eng.dispose()

    httpx_mock.add_response(
        url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
        method="GET", status_code=200, json=[],
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    loss_guard = _find_guard(daemon.safety_chain.guards, RealizedLossGuard)
    dd_guard = _find_guard(daemon.safety_chain.guards, DrawdownGuard)

    # (A) backed by the real NAV source, not the 0/0 stub, and a single shared
    #     instance so both guards see the same snapshot history.
    assert isinstance(loss_guard.source, ReconcileNavTracker)  # type: ignore[attr-defined]
    assert dd_guard.source is loss_guard.source  # type: ignore[attr-defined]

    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    # Before any reconcile: permissive (no NAV history).
    assert (await loss_guard.evaluate(_post(), ctx)).allowed is True
    assert (await dd_guard.evaluate(_post(), ctx)).allowed is True

    # (B) subscribed to the daemon bus → (C) a NAV drop makes both guards block.
    #     100 → 40 = $60 loss (> 57 threshold) and 60% drawdown (> 15% threshold).
    await daemon.bus.publish(_reconciled("100", 1_000))
    await daemon.bus.publish(_reconciled("40", 2_000))

    loss_result = await loss_guard.evaluate(_post(), ctx)
    dd_result = await dd_guard.evaluate(_post(), ctx)
    assert loss_result.allowed is False
    assert dd_result.allowed is False
