"""TradingStatusService — "will you place an order right now, and why not?"

Motivation (2026-07-27 incident, in three parts):

1. `BFX_ALLOCATION_CAP_USDT=0` was set to pause the canary. Every configured
   symbol has an explicit cap in the safety config, so the env scalar bound
   nothing; the bot kept lending for hours. The value alone read as "paused" —
   only the resolution SOURCE shows the knob was inert.
2. The pause was "verified" with `docker exec printenv` — reading back the
   input we had just written. A tautology: it cannot fail.
3. `halted` was knowable only as an env var, never as behaviour.

So this service reports behaviour, and reports it by ASKING THE REAL GUARDS,
never by re-reading configuration. These tests use real guard objects for that
reason: a fake chain would let the service and the money path drift apart,
which is the exact failure being fixed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.admin.trading_status import TradingStatusService
from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AllocationCapGuard,
    BuyingPowerGuard,
    ManualKillGuard,
)
from bfx_funding_bot.modules.execution.safety.trading_state import (
    TradingState,
    TransitionResult,
    restates,
    validate_transition,
)
from bfx_funding_bot.modules.strategy import CellConfig, StrategyName

D = Decimal


async def test_status_exposes_actual_cell_choices_for_scoped_release_requests():
    service = _service(cells=[_cell("fUST", "p2")])
    status = await service.snapshot()
    assert status["configured_cells"] == [
        {"symbol": "fUST", "cell": "fUST_p2", "strategy": "mean_reversion", "period": "p2"}]

# Module-level so they can be default arguments (B008). `None` cannot serve as
# the "not supplied" sentinel here: an unset env fallback IS None, and one test
# pins that None and Decimal("0") must not render identically.
_ENV_CAP_ZERO = D("0")
_ENV_BUFFER_3 = D("3")


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _FakeLedger:
    def __init__(
        self, *, reserved: dict[str, Decimal] | None = None,
        realized: dict[str, Decimal] | None = None,
        available: dict[str, Decimal] | None = None,
    ) -> None:
        self._reserved = reserved or {}
        self._realized = realized or {}
        self._available = available or {}

    def current_exposure(self, symbol: str) -> Decimal:
        return self.reserved_exposure(symbol) + self.realized_exposure(symbol)

    def reserved_exposure(self, symbol: str) -> Decimal:
        return self._reserved.get(symbol, D("0"))

    def realized_exposure(self, symbol: str) -> Decimal:
        return self._realized.get(symbol, D("0"))

    def available_balance(self, symbol: str) -> Decimal:
        return self._available.get(symbol, D("0"))


def _cell(symbol: str, period_agg: str = "a30") -> CellConfig:
    return CellConfig(
        strategy=StrategyName.MEAN_REVERSION, symbol=symbol, period_agg=period_agg,
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 1.0, "ema_span": 10},
        reference_amount_usdt=150.0,
        staleness_budget_hours=48,
    )


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), D("0"))


class _FakeTradingState:
    """In-memory stand-in for TradingStateRepository (its DB tests live elsewhere).

    Uses the repository's own transition rules so a test cannot get a write
    the real repository would refuse.
    """

    def __init__(self, state: TradingState | None = None) -> None:
        self.state = state
        self.writes: list[tuple[str, str, str, str]] = []

    async def current(self) -> TradingState | None:
        return self.state

    async def transition(
        self, state: str, *, cause: str, actor: str, reason: str, now_ms: int | None = None,
    ) -> TransitionResult:
        previous = self.state
        if restates(previous, state=state, cause=cause):
            assert previous is not None
            return TransitionResult(state=previous, changed=False, previous=previous)
        validate_transition(previous, state=state, cause=cause, actor=actor, reason=reason)
        self.writes.append((state, cause, reason, actor))
        self.state = TradingState(
            id=7 + len(self.writes), state=state, cause=cause, actor=actor, reason=reason,
            created_at_ms=now_ms or 1000,
        )
        return TransitionResult(state=self.state, changed=True, previous=previous)

    async def history(self, *, limit: int = 20) -> list[TradingState]:
        return [self.state] if self.state is not None else []


def _trading(
    state: str, reason: str = "candle distortion", cause: str = "operator",
) -> TradingState:
    return TradingState(
        id=7, state=state, cause=cause, reason=reason, actor="admin", created_at_ms=1000,
    )


def _halt_state(halted: bool, reason: str = "candle distortion") -> TradingState:
    """An operator's HALTED or ACTIVE: the old maintenance halt/resume."""
    return _trading("HALTED" if halted else "ACTIVE", reason=reason)


def _service(
    *,
    guards: list[Any] | None = None,
    ledger: _FakeLedger | None = None,
    caps: dict[str, Decimal] | None = None,
    buffers: dict[str, Decimal] | None = None,
    env_fallback_cap: Decimal | None = _ENV_CAP_ZERO,
    env_fallback_buffer: Decimal | None = _ENV_BUFFER_3,
    cells: list[CellConfig] | None = None,
    recorder: SubmitAttemptRecorder | None = None,
    trading_state: Any = None,
    kill_switch: Any = None,
) -> TradingStatusService:
    led = ledger if ledger is not None else _FakeLedger()
    chain = SafetyGuardChain(
        guards=guards if guards is not None else [ManualKillGuard(trading_state=trading_state)],
        probe=HealthProbe(), diagnostics=_Sink(),
        phase=Phase.SHADOW, strategy=StrategyName.MEAN_REVERSION,
        cell="c1", account_id="default",
    )
    return TradingStatusService(
        chain=chain,
        exposure=led,
        account_ctx=_ctx(),
        cells=cells if cells is not None else [_cell("fUST")],
        caps=caps if caps is not None else {"fUST": D("10000")},
        default_cap=D("0"),
        env_fallback_cap=env_fallback_cap,
        buffers=buffers if buffers is not None else {"fUST": D("3")},
        default_buffer=D("0"),
        env_fallback_buffer=env_fallback_buffer,
        phase=Phase.SHADOW,
        attempts=recorder if recorder is not None else SubmitAttemptRecorder(),
        trading_state=trading_state,
        kill_switch=kill_switch,
    )


# --------------------------------------------------------------------------
# halt state — behaviour, not env var
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halted_true_comes_from_asking_the_real_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = await _service(trading_state=_FakeTradingState(_trading("HALTED"))).snapshot()
    assert snap["halt"]["halted"] is True
    assert snap["halt"]["guard_installed"] is True
    assert "trading state HALTED" in snap["halt"]["reason"]


@pytest.mark.asyncio
async def test_halted_false_when_flag_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = await _service().snapshot()
    assert snap["halt"]["halted"] is False
    assert snap["halt"]["reason"] is None


@pytest.mark.asyncio
async def test_uninstalled_kill_guard_is_not_reported_as_running_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`guard_installed=False` must be visible. "No guard blocked" and "no guard
    exists to block" are different states; conflating them is how a disabled
    safety control reads as a healthy one."""
    snap = await _service(guards=[],
                          trading_state=_FakeTradingState(_trading("HALTED"))).snapshot()
    assert snap["halt"]["guard_installed"] is False
    assert snap["halt"]["halted"] is False
    assert "not installed" in (snap["halt"]["note"] or "")


@pytest.mark.asyncio
async def test_installed_guards_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = await _service(
        guards=[
            ManualKillGuard(),
            AllocationCapGuard(
                ledger=_FakeLedger(), caps={"fUST": D("10000")}, default_cap=D("0"),
            ),
        ],
    ).snapshot()
    assert [g["name"] for g in snap["guards"]] == ["manual_kill", "allocation_cap"]


# --------------------------------------------------------------------------
# effective config — value AND which tier bound it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_reports_value_and_source() -> None:
    snap = await _service().snapshot()
    cap = snap["symbols"]["fUST"]["cap"]
    assert cap["value"] == "10000"
    assert cap["source"] == "symbol_map"


@pytest.mark.asyncio
async def test_env_fallback_cap_reports_non_binding_when_every_symbol_is_explicit() -> None:
    """THE regression test for 2026-07-27: value 0 present, binds nothing."""
    snap = await _service(env_fallback_cap=D("0")).snapshot()
    fb = snap["env_fallback_cap"]
    assert fb["value"] == "0"
    assert fb["binding"] is False
    assert fb["binding_symbols"] == []
    assert "explicit" in fb["why"]


@pytest.mark.asyncio
async def test_env_fallback_cap_reports_binding_when_a_symbol_has_no_entry() -> None:
    snap = await _service(
        cells=[_cell("fUST"), _cell("fUSD")],
        caps={"fUST": D("10000")},          # fUSD absent → falls through
        buffers={"fUST": D("3")},
        env_fallback_cap=D("400"),
    ).snapshot()
    fb = snap["env_fallback_cap"]
    assert fb["binding"] is True
    assert fb["binding_symbols"] == ["fUSD"]
    assert snap["symbols"]["fUSD"]["cap"] == {"value": "400", "source": "env_fallback"}


@pytest.mark.asyncio
async def test_unset_env_fallback_is_reported_as_null_not_zero() -> None:
    """None (unset) and Decimal('0') (set to zero) are different configurations
    and must not render identically."""
    snap = await _service(env_fallback_cap=None).snapshot()
    assert snap["env_fallback_cap"]["value"] is None


# --------------------------------------------------------------------------
# funds — the reason a halt can be unverifiable
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reports_balance_exposure_and_deployable_headroom() -> None:
    snap = await _service(
        ledger=_FakeLedger(
            reserved={"fUST": D("100")}, realized={"fUST": D("400")},
            available={"fUST": D("3.00")},
        ),
    ).snapshot()
    s = snap["symbols"]["fUST"]
    assert s["available_balance"] == "3.00"
    assert s["exposure"] == {"reserved": "100", "realized": "400", "total": "500"}
    # available 3.00 − buffer 3 = 0 → nothing deployable; this is exactly why
    # "no orders appeared" proved nothing about the halt on 2026-07-27.
    assert s["deployable_headroom"] == "0.00"


@pytest.mark.asyncio
async def test_deployable_headroom_floors_at_zero_never_negative() -> None:
    snap = await _service(
        ledger=_FakeLedger(available={"fUST": D("1")}),
        buffers={"fUST": D("3")},
    ).snapshot()
    assert snap["symbols"]["fUST"]["deployable_headroom"] == "0"


# --------------------------------------------------------------------------
# last attempt
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_submit_attempt_is_null_with_process_start_when_nothing_tried() -> None:
    snap = await _service().snapshot()
    assert snap["last_submit_attempt"] is None
    # Without this, null reads as "never traded" instead of "not since boot".
    assert snap["process_started_at"] is not None


@pytest.mark.asyncio
async def test_last_submit_attempt_surfaces_the_blocking_guard() -> None:
    rec = SubmitAttemptRecorder(
        clock=lambda: datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
    )
    rec.record_blocked(
        cell="fUST_a30", symbol="fUST", amount=D("150"),
        guard_name="manual_kill", reason="trading state HALTED: operator stop",
    )
    snap = await _service(recorder=rec).snapshot()
    assert snap["last_submit_attempt"]["outcome"] == "blocked"
    assert snap["last_submit_attempt"]["guard_name"] == "manual_kill"


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_reports_the_halt_even_with_no_funds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadlock this breaks: with available=3.00 the reconciler never sizes
    an offer, so the kill switch could not be verified by observation. The probe
    asks directly."""
    halted = _FakeTradingState(_trading("HALTED"))
    svc = _service(
        trading_state=halted,
        guards=[
            ManualKillGuard(trading_state=halted),
            BuyingPowerGuard(
                ledger=_FakeLedger(available={"fUST": D("3.00")}),
                buffers={"fUST": D("3")}, default_buffer=D("0"),
            ),
        ],
        ledger=_FakeLedger(available={"fUST": D("3.00")}),
    )
    out = await svc.dry_run()
    assert out["would_submit_any"] is False
    fust = out["symbols"]["fUST"]
    assert fust["would_submit"] is False
    assert fust["blocked_by"] == "manual_kill"
    # And the full picture: funds would ALSO block, so clearing the kill switch
    # alone would not resume trading.
    assert [g["name"] for g in fust["guards"] if not g["allowed"]] == [
        "manual_kill", "buying_power",
    ]


@pytest.mark.asyncio
async def test_dry_run_probes_every_configured_symbol_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live finding, 2026-07-27: defaulting to one symbol picked fUSD (sorted
    first, and dark — zero balance), whose verdict was "blocked by buying_power".
    An operator running the default probe would have concluded funds were the
    blocker while the funded symbol was held solely by the kill switch. A probe
    that reports on a symbol nobody is trading is worse than none."""
    svc = _service(
        trading_state=_FakeTradingState(_trading("HALTED")),
        cells=[_cell("fUST"), _cell("fUSD")],
        caps={"fUST": D("10000"), "fUSD": D("400")},
        buffers={"fUST": D("3"), "fUSD": D("3")},
    )
    out = await svc.dry_run()
    assert sorted(out["symbols"]) == ["fUSD", "fUST"]
    assert all(s["blocked_by"] == "manual_kill" for s in out["symbols"].values())


@pytest.mark.asyncio
async def test_would_submit_any_is_true_when_any_single_symbol_would_trade() -> None:
    """The headline field must answer "could this daemon place ANY order right
    now" — one tradeable symbol is enough for the answer to be yes."""
    svc = _service(
        cells=[_cell("fUST"), _cell("fUSD")],
        caps={"fUST": D("10000"), "fUSD": D("400")},
        buffers={"fUST": D("3"), "fUSD": D("3")},
        guards=[BuyingPowerGuard(
            ledger=_FakeLedger(available={"fUST": D("500"), "fUSD": D("0")}),
            buffers={"fUST": D("3"), "fUSD": D("3")}, default_buffer=D("0"),
        )],
    )
    out = await svc.dry_run()
    assert out["symbols"]["fUSD"]["would_submit"] is False
    assert out["symbols"]["fUST"]["would_submit"] is True
    assert out["would_submit_any"] is True


@pytest.mark.asyncio
async def test_dry_run_accepts_an_explicit_symbol_and_reports_only_that_one() -> None:
    svc = _service(
        cells=[_cell("fUST"), _cell("fUSD")],
        caps={"fUST": D("10000"), "fUSD": D("400")},
        buffers={"fUST": D("3"), "fUSD": D("3")},
    )
    out = await svc.dry_run(symbol="fUST")
    assert list(out["symbols"]) == ["fUST"]


@pytest.mark.asyncio
async def test_dry_run_echoes_the_synthetic_decision_it_evaluated() -> None:
    svc = _service()
    out = await svc.dry_run(symbol="fUST", amount=250.0, rate=0.0002, period_days=7)
    d = out["symbols"]["fUST"]["decision"]
    assert d["symbol"] == "fUST"
    assert d["offer_amount_usdt"] == "250.0"  # exact decimal strings
    assert d["offer_rate"] == "0.0002"
    assert d["offer_duration_days"] == 7


@pytest.mark.asyncio
async def test_dry_run_defaults_come_from_the_configured_cells_not_magic_numbers() -> None:
    svc = _service(cells=[_cell("fUST")])
    out = await svc.dry_run()
    d = out["symbols"]["fUST"]["decision"]
    assert d["offer_amount_usdt"] == "150.0"  # cell reference_amount_usdt


@pytest.mark.asyncio
async def test_dry_run_rejects_an_unconfigured_symbol() -> None:
    """Probing a symbol the daemon does not trade would return a verdict that
    describes nothing real."""
    svc = _service(cells=[_cell("fUST")])
    with pytest.raises(ValueError, match="fBTC"):
        await svc.dry_run(symbol="fBTC")


@pytest.mark.asyncio
async def test_dry_run_is_a_post_decision_so_the_sizing_guards_actually_run() -> None:
    """SKIP decisions bypass AllocationCapGuard and BuyingPowerGuard entirely —
    a probe built from one would report "nothing blocks" no matter the caps."""
    svc = _service(
        guards=[AllocationCapGuard(
            ledger=_FakeLedger(realized={"fUST": D("10000")}),
            caps={"fUST": D("10000")}, default_cap=D("0"),
        )],
    )
    out = await svc.dry_run()
    fust = out["symbols"]["fUST"]
    assert fust["decision"]["decision_outcome"] == "post"
    assert fust["would_submit"] is False
    assert fust["blocked_by"] == "allocation_cap"


@pytest.mark.asyncio
async def test_dry_run_emits_no_safety_trigger() -> None:
    sink = _Sink()
    chain = SafetyGuardChain(
        guards=[ManualKillGuard()], probe=HealthProbe(), diagnostics=sink,
        phase=Phase.SHADOW, strategy=StrategyName.MEAN_REVERSION,
        cell="c1", account_id="default",
    )
    svc = TradingStatusService(
        chain=chain, exposure=_FakeLedger(), account_ctx=_ctx(), cells=[_cell("fUST")],
        caps={"fUST": D("10000")}, default_cap=D("0"), env_fallback_cap=D("0"),
        buffers={"fUST": D("3")}, default_buffer=D("0"), env_fallback_buffer=D("3"),
        phase=Phase.SHADOW, attempts=SubmitAttemptRecorder(),
    )
    await svc.dry_run()
    await svc.snapshot()
    assert sink.events == []


# --------------------------------------------------------------------------
# persisted halt (P2) — the halt must survive a canary.env revert
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_the_persisted_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = await _service(trading_state=_FakeTradingState(_halt_state(True))).snapshot()
    assert snap["halt"]["halted"] is True
    persisted = snap["halt"]["sources"]["persisted"]
    assert persisted["halted"] is True
    assert persisted["state"] == "HALTED"
    assert persisted["reason"] == "candle distortion"
    assert persisted["actor"] == "admin"
    assert persisted["id"] == 7


@pytest.mark.asyncio
async def test_never_configured_persisted_state_is_null_and_reads_as_halted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`null` (no decision ever recorded) and `{"halted": false}` (explicitly
    ACTIVE, with a reason) are different facts; the first fails closed."""
    snap = await _service(trading_state=_FakeTradingState(None)).snapshot()
    assert snap["halt"]["sources"]["persisted"] is None
    assert snap["halt"]["halted"] is True


class _FakeKillSwitch:
    def __init__(self, store: _FakeTradingState, *, complete: bool) -> None:
        self.store, self.complete, self.calls = store, complete, []

    async def engage(self, *, cause: str, actor: str, reason: str) -> Any:
        from bfx_funding_bot.modules.execution.safety.kill_switch import (
            CancelAllOutcome,
            KillResult,
        )
        self.calls.append((cause, actor, reason))
        result = await self.store.transition("HALTED", cause=cause, actor=actor, reason=reason)
        phase = "acknowledged" if self.complete else "failed"
        return KillResult(state=result.state, state_changed=result.changed,
                          cancel_all=(CancelAllOutcome("UST", phase, None if self.complete else "down"),))


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_halt_is_the_operator_kill_and_reports_the_venue_part(complete: bool) -> None:
    store = _FakeTradingState(_trading("ACTIVE"))
    kill = _FakeKillSwitch(store, complete=complete)
    svc = _service(trading_state=store, kill_switch=kill)
    out = await svc.halt(reason="venue incident", actor="will")
    assert kill.calls == [("operator", "will", "venue incident")]
    assert out["state"] == "HALTED" and out["cause"] == "operator"
    assert out["cancel_all_complete"] is complete
    assert out["cancel_all"][0]["currency"] == "UST"


@pytest.mark.asyncio
async def test_halt_without_a_store_is_a_clear_error_not_a_silent_noop() -> None:
    """paper/shadow have no store. Silently accepting a halt request there
    would report success while changing nothing."""
    svc = _service(trading_state=None)
    with pytest.raises(ValueError, match="not configured"):
        await svc.halt(reason="x", actor="admin")


@pytest.mark.asyncio
async def test_status_includes_recent_halt_history() -> None:
    """"Who resumed trading and why" must be answerable from the same place
    that answers "are we halted"."""
    snap = await _service(trading_state=_FakeTradingState(_halt_state(True))).snapshot()
    assert snap["halt"]["history"][0]["reason"] == "candle distortion"


def test_the_static_token_cannot_resume() -> None:
    """ADR D4: resuming needs the operator's TOTP; a static token never lifts a stop."""
    assert not hasattr(TradingStatusService, "resume")
