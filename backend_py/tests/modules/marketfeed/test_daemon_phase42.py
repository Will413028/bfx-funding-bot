"""build_daemon: AccountContext + executor + chain + conditional fill_tracker."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pytest_httpx import HTTPXMock

from bfx_funding_bot.core.errors import (
    ExecutorAuthError,
    ExecutorTransientError,
)
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
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
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


def _write_safety_yaml(tmp_path: Path, *, disable: set[str] | None = None) -> Path:
    """Write a safety.yaml with `disable` set marking which guards to flip off.

    Names: manual_kill / auth_health / heartbeat / allocation_cap /
    realized_loss_24h / drawdown_from_peak / divergence_rate.
    """
    disable = disable or set()

    def b(name: str, default: bool) -> str:
        return "false" if name in disable else ("true" if default else "false")

    path = tmp_path / "safety.yaml"
    path.write_text(f"""
hard_guards:
  manual_kill:
    enabled: {b("manual_kill", True)}
  auth_health:
    enabled: {b("auth_health", True)}
  heartbeat:
    enabled: {b("heartbeat", True)}
    sub_task_stale_threshold_seconds: 300
  allocation_cap:
    enabled: {b("allocation_cap", True)}
calibrated_guards:
  realized_loss_24h:
    enabled: false
    threshold_usdt: null
  drawdown_from_peak:
    enabled: false
    threshold_pct: null
  divergence_rate:
    enabled: false
    threshold_pct: null
    window_minutes: null
""")
    return path


@pytest.mark.asyncio
async def test_build_daemon_filters_disabled_hard_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """M2 regression: SafetyConfig.<guard>.enabled flag was Pydantic-parsed
    but build_daemon ignored it — 7 guards always constructed regardless of
    yaml. Lock the contract: build_daemon must filter guards by enabled."""
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    # Disable heartbeat + allocation_cap from hard_guards.
    safety_yaml = _write_safety_yaml(
        tmp_path, disable={"heartbeat", "allocation_cap"},
    )
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_yaml))

    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
        DivergenceRateGuard,
        DrawdownGuard,
        RealizedLossGuard,
    )
    from bfx_funding_bot.modules.execution.safety.hard_guards import (
        AllocationCapGuard,
        AuthHealthGuard,
        HeartbeatGuard,
        ManualKillGuard,
    )
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    guards = daemon.safety_chain.guards
    types = {type(g) for g in guards}

    # Enabled hard guards present.
    assert ManualKillGuard in types
    assert AuthHealthGuard in types
    # Disabled hard guards absent.
    assert HeartbeatGuard not in types
    assert AllocationCapGuard not in types
    # All calibrated guards disabled in this fixture → absent.
    assert RealizedLossGuard not in types
    assert DrawdownGuard not in types
    assert DivergenceRateGuard not in types


@pytest.mark.asyncio
async def test_build_daemon_includes_enabled_calibrated_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """M2: an enabled calibrated guard (with a valid threshold) appears in
    the chain. Pairs with the disabled-default safety.yaml fixture."""
    _base_env(monkeypatch)
    monkeypatch.setenv("BFX_ACCOUNT_ID", "default")
    monkeypatch.setenv("BFX_API_KEY", "test_key")
    monkeypatch.setenv("BFX_API_SECRET", "test_secret")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "500")
    monkeypatch.delenv("BFX_EXECUTOR", raising=False)
    monkeypatch.delenv("BFX_FILL_TRACKER_ENABLED", raising=False)

    safety_yaml = tmp_path / "safety.yaml"
    safety_yaml.write_text("""
hard_guards:
  manual_kill: {enabled: true}
  auth_health: {enabled: true}
  heartbeat: {enabled: true, sub_task_stale_threshold_seconds: 300}
  allocation_cap: {enabled: true}
calibrated_guards:
  realized_loss_24h:
    enabled: true
    threshold_usdt: 100.0
  drawdown_from_peak:
    enabled: false
    threshold_pct: null
  divergence_rate:
    enabled: false
    threshold_pct: null
    window_minutes: null
""")
    monkeypatch.setenv("BFX_SAFETY_CONFIG", str(safety_yaml))

    httpx_mock.add_response(
        url="https://api.axiom.co/v1/datasets/x/ingest",
        method="POST", status_code=200, json={"ingested": 1},
        is_reusable=True, is_optional=True,
    )

    from bfx_funding_bot.modules.execution.safety.calibrated_guards import (
        DrawdownGuard,
        RealizedLossGuard,
    )
    from bfx_funding_bot.modules.marketfeed.daemon import build_daemon
    daemon = await build_daemon(
        cells_yaml_path=_write_cells_yaml(tmp_path), skip_ws=True,
    )

    types = {type(g) for g in daemon.safety_chain.guards}
    assert RealizedLossGuard in types  # enabled
    assert DrawdownGuard not in types  # disabled


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


class _SuccessSubmitExecutor:
    """Stub: returns status='filled'. Mirrors EchoPaperExecutor happy path."""

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
        return SubmittedOrder(
            cid=54321,
            venue_offer_id="venue-xyz",
            status="filled",
            raw_response={},
        )


def _make_decision_ctx() -> tuple[DecisionPayload, AccountContext]:
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
    return decision, ctx


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
    probe = HealthProbe()
    wrapped = _LedgerWrappedExecutor(_FailedSubmitExecutor(), ledger, probe)

    decision, ctx = _make_decision_ctx()
    result = await wrapped.submit(decision, ctx)

    assert result.status == "failed"
    # Exposure unchanged — failed submit did not call on_order_fill.
    assert ledger.current_exposure() == Decimal("0")
    # Heartbeat still fires even on failed submit — liveness signal is
    # independent of business outcome (the wrapper completed its work).
    assert "executor" in probe.last_active_ts


@pytest.mark.asyncio
async def test_ledger_wrapped_executor_records_executor_heartbeat() -> None:
    """I1 follow-up: _LedgerWrappedExecutor.submit must record executor
    heartbeat after the inner submit returns.

    HeartbeatGuard watches ["safety_chain", "executor"]; before this fix
    nothing recorded the "executor" key so the watchdog was half-blind.
    """
    ledger = PaperPositionLedger(account_id="default")
    probe = HealthProbe()
    wrapped = _LedgerWrappedExecutor(_SuccessSubmitExecutor(), ledger, probe)

    assert "executor" not in probe.last_active_ts

    decision, ctx = _make_decision_ctx()
    result = await wrapped.submit(decision, ctx)

    assert result.status == "filled"
    assert "executor" in probe.last_active_ts


class _TransientThenSuccessExecutor:
    """Stub: raises ExecutorTransientError fail_count times, then returns
    a filled order. Used to prove that _LedgerWrappedExecutor retries
    transient errors instead of letting them propagate and kill the daemon.
    """

    def __init__(self, fail_count: int = 2) -> None:
        self._fail_count = fail_count
        self.calls = 0

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
        self.calls += 1
        if self.calls <= self._fail_count:
            raise ExecutorTransientError(f"flake #{self.calls}")
        return SubmittedOrder(
            cid=99999,
            venue_offer_id="venue-after-retry",
            status="filled",
            raw_response={},
        )


class _AlwaysTransientExecutor:
    """Stub: every submit raises ExecutorTransientError."""

    def __init__(self) -> None:
        self.calls = 0

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
        self.calls += 1
        raise ExecutorTransientError("persistent")


class _AlwaysFatalExecutor:
    """Stub: every submit raises ExecutorAuthError (fatal, must NOT retry)."""

    def __init__(self) -> None:
        self.calls = 0

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> SubmittedOrder:
        self.calls += 1
        raise ExecutorAuthError("401")


@pytest.mark.asyncio
async def test_ledger_wrapped_executor_retries_transient_then_succeeds() -> None:
    """I2 follow-up: ExecutorTransientError must trigger transient_retry,
    not propagate up and kill the daemon.

    Before the fix, executor.submit was unwrapped — any exception from the
    inner submit (including transient httpx network errors) would bubble
    out of SignalEngine and tear down the daemon TaskGroup. After wrapping
    with transient_retry, transient failures retry 3 times before re-raising.
    """
    ledger = PaperPositionLedger(account_id="default")
    probe = HealthProbe()
    inner = _TransientThenSuccessExecutor(fail_count=2)
    wrapped = _LedgerWrappedExecutor(inner, ledger, probe)

    decision, ctx = _make_decision_ctx()
    result = await wrapped.submit(decision, ctx)

    assert inner.calls == 3  # 2 transient failures + 1 success
    assert result.status == "filled"
    # Ledger updates once with the final successful fill (not 3 times).
    assert ledger.current_exposure() == Decimal("150")
    # Heartbeat fires once after the eventual success.
    assert "executor" in probe.last_active_ts


@pytest.mark.asyncio
async def test_ledger_wrapped_executor_does_not_retry_fatal() -> None:
    """I2 follow-up: ExecutorAuthError (and other fatals) must propagate
    immediately without triggering retries. Auth failures need to escalate
    to sys.exit(78) — silently retrying would mask credential rot.
    """
    ledger = PaperPositionLedger(account_id="default")
    probe = HealthProbe()
    inner = _AlwaysFatalExecutor()
    wrapped = _LedgerWrappedExecutor(inner, ledger, probe)

    decision, ctx = _make_decision_ctx()
    with pytest.raises(ExecutorAuthError):
        await wrapped.submit(decision, ctx)

    assert inner.calls == 1  # no retry
    # Failed submit must not update ledger or fire heartbeat.
    assert ledger.current_exposure() == Decimal("0")
    assert "executor" not in probe.last_active_ts


@pytest.mark.asyncio
async def test_ledger_wrapped_executor_reraises_after_transient_exhausted() -> None:
    """I2 follow-up: after N transient retries fail, the final
    ExecutorTransientError must re-raise so the daemon can decide to
    escalate (sustained outage = not a paper-safe situation).
    """
    ledger = PaperPositionLedger(account_id="default")
    probe = HealthProbe()
    inner = _AlwaysTransientExecutor()
    wrapped = _LedgerWrappedExecutor(inner, ledger, probe)

    decision, ctx = _make_decision_ctx()
    with pytest.raises(ExecutorTransientError):
        await wrapped.submit(decision, ctx)

    # transient_retry caps at RETRY_ATTEMPTS (3).
    assert inner.calls == 3
    # Ledger NOT updated; heartbeat NOT fired (no successful inner submit).
    assert ledger.current_exposure() == Decimal("0")
    assert "executor" not in probe.last_active_ts
