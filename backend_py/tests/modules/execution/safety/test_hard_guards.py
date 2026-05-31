"""Hard guards: ManualKill + AuthHealth + Heartbeat (first 3 of 4)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
)
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AllocationCapGuard,
    AuthHealthGuard,
    BuyingPowerGuard,
    HeartbeatGuard,
    ManualKillGuard,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    HealthStatus,
    HealthTarget,
)


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _post(symbol: str = "fUST") -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
        symbol=symbol,
    )


@pytest.mark.asyncio
async def test_manual_kill_allows_when_flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BFX_KILL_SWITCH", raising=False)
    g = ManualKillGuard()
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_manual_kill_blocks_when_flag_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BFX_KILL_SWITCH", "true")
    g = ManualKillGuard()
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert r.guard_name == "manual_kill"


@pytest.mark.asyncio
async def test_manual_kill_is_not_calibrated() -> None:
    assert ManualKillGuard().is_calibrated is False


@pytest.mark.asyncio
async def test_auth_health_allows_when_executor_healthy() -> None:
    probe = HealthProbe()
    probe.update(HealthTarget.EXECUTOR, HealthStatus.HEALTHY)
    g = AuthHealthGuard(probe=probe)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_auth_health_blocks_when_executor_down() -> None:
    probe = HealthProbe()
    probe.update(HealthTarget.EXECUTOR, HealthStatus.DOWN, error_message="x")
    g = AuthHealthGuard(probe=probe)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "executor" in (r.reason or "")


@pytest.mark.asyncio
async def test_auth_health_allows_when_target_never_set() -> None:
    # Day-1: executor target may not have been updated yet — allow by default.
    probe = HealthProbe()
    g = AuthHealthGuard(probe=probe)
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_heartbeat_allows_when_all_fresh() -> None:
    probe = HealthProbe()
    probe.record_heartbeat("safety_chain")
    probe.record_heartbeat("executor")
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["safety_chain", "executor"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_heartbeat_blocks_when_any_stale() -> None:
    probe = HealthProbe()
    probe.last_active_ts["safety_chain"] = datetime.now(UTC)
    probe.last_active_ts["executor"] = datetime.now(UTC) - timedelta(seconds=400)
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["safety_chain", "executor"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is False
    assert "executor" in (r.reason or "")


@pytest.mark.asyncio
async def test_heartbeat_allows_when_target_never_recorded() -> None:
    # Day-1 boot: heartbeat dict may not have the key yet.
    probe = HealthProbe()
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["safety_chain"])
    r = await g.evaluate(_post(), _ctx())
    assert r.allowed is True


@pytest.mark.asyncio
async def test_heartbeat_edge_at_exactly_threshold() -> None:
    probe = HealthProbe()
    probe.last_active_ts["x"] = datetime.now(UTC) - timedelta(seconds=300)
    g = HeartbeatGuard(probe=probe, threshold_seconds=300,
                       watched_sub_tasks=["x"])
    r = await g.evaluate(_post(), _ctx())
    # Exactly at threshold = still allowed; strictly greater blocks.
    assert r.allowed is True


class _FakeLedger:
    def __init__(self, exposures: dict[str, Decimal] | None = None) -> None:
        self._exposures = exposures or {}

    def current_exposure(self, symbol: str) -> Decimal:
        return self._exposures.get(symbol, Decimal("0"))


@pytest.mark.asyncio
async def test_allocation_cap_allows_under_cap() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    ledger = _FakeLedger({"fUST": Decimal("100")})
    g = AllocationCapGuard(ledger=ledger)
    decision = _post()  # offer_amount_usdt=100, symbol=fUST
    r = await g.evaluate(decision, ctx)
    assert r.allowed is True


@pytest.mark.asyncio
async def test_allocation_cap_blocks_over_cap() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    ledger = _FakeLedger({"fUST": Decimal("450")})
    g = AllocationCapGuard(ledger=ledger)
    decision = _post()  # 100 → 450+100=550 > 500
    r = await g.evaluate(decision, ctx)
    assert r.allowed is False
    assert "cap" in (r.reason or "")
    assert "symbol=fUST" in (r.reason or "")


@pytest.mark.asyncio
async def test_allocation_cap_edge_at_exactly_cap() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    ledger = _FakeLedger({"fUST": Decimal("400")})  # 400 + 100 = 500 (exactly)
    g = AllocationCapGuard(ledger=ledger)
    r = await g.evaluate(_post(), ctx)
    # Exactly at cap = allowed; strictly over blocks.
    assert r.allowed is True


@pytest.mark.asyncio
async def test_allocation_cap_skip_decision_always_allowed() -> None:
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("100"))
    ledger = _FakeLedger({"fUST": Decimal("99999")})
    g = AllocationCapGuard(ledger=ledger)
    skip = DecisionPayload(
        decision_outcome=DecisionOutcome.SKIP,
        signal_correlation_id=uuid4(),
        skip_reason="below_threshold",
        symbol="fUST",
    )
    r = await g.evaluate(skip, ctx)
    assert r.allowed is True  # SKIP decisions never consume cap


@pytest.mark.asyncio
async def test_allocation_cap_isolates_buckets_per_symbol() -> None:
    # fUST bucket is full (over cap) but the empty fUSD bucket must allow a
    # fUSD POST: the guard reads ONLY decision.symbol's exposure, never a sum.
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    ledger = _FakeLedger({"fUST": Decimal("500")})  # fUSD absent → reads 0
    g = AllocationCapGuard(ledger=ledger)

    blocked = await g.evaluate(_post(symbol="fUST"), ctx)  # 500+100=600 > 500
    assert blocked.allowed is False

    allowed = await g.evaluate(_post(symbol="fUSD"), ctx)  # 0+100=100 <= 500
    assert allowed.allowed is True


class _FakeBalanceLedger:
    def __init__(self, balances: dict[str, Decimal] | None = None) -> None:
        self._balances = balances or {}

    def available_balance(self, symbol: str) -> Decimal:
        return self._balances.get(symbol, Decimal("0"))


def _post_decision(amount: float | None, symbol: str = "fUST") -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.00012,
        offer_amount_usdt=amount,
        offer_duration_days=2,
        symbol=symbol,
    )


def _bp_ctx() -> AccountContext:
    return AccountContext(
        account_id="default",
        credentials=Credentials(api_key="k", api_secret="s"),
        allocation_cap_usdt=Decimal("570"),
    )


@pytest.mark.asyncio
async def test_buying_power_blocks_over_available() -> None:
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("150")}), buffer_usdt=Decimal("3"))
    # deployable = 150 - 3 = 147; offer 160 > 147 -> block
    res = await guard.evaluate(_post_decision(160.0), _bp_ctx())
    assert res.allowed is False
    assert res.guard_name == "buying_power"


@pytest.mark.asyncio
async def test_buying_power_allows_within_available() -> None:
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("250")}), buffer_usdt=Decimal("3"))
    res = await guard.evaluate(_post_decision(200.0), _bp_ctx())
    assert res.allowed is True


@pytest.mark.asyncio
async def test_buying_power_skip_bypasses() -> None:
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("0")}), buffer_usdt=Decimal("3"))
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.SKIP,
        signal_correlation_id=uuid4(),
        skip_reason="below_threshold",
        offer_rate=None, offer_amount_usdt=None, offer_duration_days=None,
        symbol="fUST",
    )
    res = await guard.evaluate(decision, _bp_ctx())
    assert res.allowed is True


@pytest.mark.asyncio
async def test_buying_power_missing_amount_blocks() -> None:
    guard = BuyingPowerGuard(ledger=_FakeBalanceLedger({"fUST": Decimal("250")}), buffer_usdt=Decimal("3"))
    # A POST DecisionPayload normally can't carry a None amount (model validator
    # rejects it); model_construct bypasses validation to exercise the guard's
    # defensive missing-amount branch directly.
    decision = DecisionPayload.model_construct(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.00012,
        offer_amount_usdt=None,
        offer_duration_days=2,
        symbol="fUST",
    )
    res = await guard.evaluate(decision, _bp_ctx())
    assert res.allowed is False


# ---------------------------------------------------------------------------
# float↔Decimal bridge invariant
#
# The reconciler sizes offers as Decimal then crosses the DecisionPayload float
# field (reconciler.py: `offer_amount_usdt=float(amount)`); the balance/cap
# guards reconstruct the Decimal via `Decimal(str(decision.offer_amount_usdt))`
# (hard_guards.py). Pin that this float→str→Decimal round-trip is exact at
# operating magnitudes so the guards compare the *sized* amount, never a
# precision-drifted one. The str() step is load-bearing: Decimal(float_value)
# would drift (e.g. Decimal(406.89) == 406.8899999999999863...).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sized",
    [
        Decimal("200"), Decimal("399"), Decimal("171"), Decimal("317"),
        Decimal("247"), Decimal("553.50"), Decimal("406.89"), Decimal("163.11"),
        Decimal("549.99"), Decimal("0.01"),
    ],
)
def test_offer_amount_float_bridge_is_lossless(sized: Decimal) -> None:
    # Mirrors reconciler write (float) + guard read (Decimal(str(...))). The
    # fractional params (406.89, 163.11, 549.99, 0.01) are the ones a direct
    # Decimal(float_value) read would drift on — they give this invariant teeth.
    bridged = Decimal(str(float(sized)))
    assert bridged == sized


def decimal_to_payload_float(amount: Decimal) -> float:
    """The exact write-side step the reconciler performs (reconciler.py:141)."""
    return float(amount)


@pytest.mark.asyncio
async def test_buying_power_exact_at_fractional_boundary_via_float_bridge() -> None:
    # available 409.89, buffer 3 -> deployable 406.89 (exact Decimal math). An
    # offer sized at exactly 406.89, after the float bridge, must be allowed; one
    # cent over must block. Exercises the real guard at a *fractional* boundary
    # (existing boundary tests only used integer amounts).
    guard = BuyingPowerGuard(
        ledger=_FakeBalanceLedger({"fUST": Decimal("409.89")}), buffer_usdt=Decimal("3"),
    )
    at = await guard.evaluate(
        _post_decision(decimal_to_payload_float(Decimal("406.89"))), _bp_ctx(),
    )
    assert at.allowed is True
    over = await guard.evaluate(
        _post_decision(decimal_to_payload_float(Decimal("406.90"))), _bp_ctx(),
    )
    assert over.allowed is False


@pytest.mark.asyncio
async def test_buying_power_isolates_buckets_per_symbol() -> None:
    # fUST funding wallet is empty but fUSD has funds: a fUST POST must block
    # while a fUSD POST of the same size is allowed. The guard reads ONLY
    # decision.symbol's available balance — never a cross-symbol sum.
    guard = BuyingPowerGuard(
        ledger=_FakeBalanceLedger({"fUSD": Decimal("250")}),  # fUST absent → 0
        buffer_usdt=Decimal("3"),
    )
    blocked = await guard.evaluate(_post_decision(100.0, symbol="fUST"), _bp_ctx())
    assert blocked.allowed is False  # 0 - 3 = -3; 100 > -3 → block

    allowed = await guard.evaluate(_post_decision(100.0, symbol="fUSD"), _bp_ctx())
    assert allowed.allowed is True   # 250 - 3 = 247; 100 <= 247 → allow
