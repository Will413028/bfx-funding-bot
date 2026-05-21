"""build_daemon: AccountContext + executor + chain + conditional fill_tracker."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.modules.execution.ledger import PaperPositionLedger
from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.registry import ExecutorConfigError
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.marketfeed.daemon import _LedgerWrappedExecutor
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


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the env baseline used by existing test_daemon.py."""
    monkeypatch.setenv("BFX_PHASE", "paper")
    monkeypatch.setenv("AXIOM_API_KEY", "x")
    monkeypatch.setenv("AXIOM_DATASET", "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("BFX_HEALTHZ_PORT", "0")


@pytest.mark.asyncio
async def test_build_daemon_wires_paper_executor_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )
    assert isinstance(daemon.executor, EchoPaperExecutor)
    assert daemon.account_ctx.account_id == "default"
    assert daemon.account_ctx.allocation_cap_usdt == Decimal("500")
    assert isinstance(daemon.safety_chain, SafetyGuardChain)
    assert daemon.fill_tracker is None


@pytest.mark.asyncio
async def test_build_daemon_invalid_executor_combo_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "k")
    monkeypatch.setenv("BFX_API_SECRET", "s")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.setenv("BFX_EXECUTOR", "paper")
    monkeypatch.setenv("BFX_FILL_TRACKER_ENABLED", "true")
    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    with pytest.raises(ExecutorConfigError):
        await build_daemon(
            cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
        )


class _FailedSubmitExecutor:
    """Stub: always returns status='failed'. Mirrors what 4.4 bitfinex_live
    will produce on venue rejection (insufficient balance / API error / etc).
    """

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
        return SubmittedOrder(
            cid=12345,
            venue_offer_id=None,
            status="failed",
            raw_response={"error": "stub failure"},
        )


@pytest.mark.asyncio
async def test_ledger_wrapped_executor_skips_failed_status() -> None:
    """Regression: failed submit must NOT inflate ledger exposure.

    Latent bug fixed in Phase 4.2 review — EchoPaperExecutor (4.2) always
    returns status='filled' so the bug doesn't fire today, but 4.4
    bitfinex_live can return 'failed' on venue rejection. Without the
    status guard, AllocationCapGuard would wrongly block subsequent POST
    decisions.
    """
    ledger = PaperPositionLedger(account_id="default")
    wrapped = _LedgerWrappedExecutor(_FailedSubmitExecutor(), ledger)

    ctx = AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("500"),
    )
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0005,
        offer_amount_usdt=150.0,
        offer_duration_days=2,
    )

    result = await wrapped.submit(decision, ctx)

    assert result.status == "failed"
    # Exposure unchanged — failed submit did not call on_order_fill.
    assert ledger.current_exposure() == Decimal("0")
