"""SafetyGuardChain.dry_evaluate — behaviour probe for the safety chain.

Motivation (2026-07-27 incident): the kill switch was set, but with
available=3.00 the reconciler never sized an offer, so the chain was never
reached and "no orders appeared" proved nothing about whether the halt worked.
The only observable was the env var — i.e. the INPUT, not the behaviour. This
probe runs a synthetic POST through the real guard list on demand, so the halt
can be verified without waiting for a credit to mature.

Contract:
- runs EVERY guard (no short-circuit) so one report shows the full picture;
- `would_submit` still equals the real short-circuiting verdict;
- `blocked_by` names the guard the real path would stop at;
- emits NOTHING (no safety_trigger) and records NO heartbeat — a probe must
  not forge evidence of trading activity;
- never touches the executor.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    GuardResult,
)
from bfx_funding_bot.modules.execution.safety.chain import (
    GUARD_EVAL_TIMEOUT_SECONDS,
    SafetyGuardChain,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    Phase,
    StrategyName,
)


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


def _post() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001, offer_amount_usdt=100.0, offer_duration_days=2,
        symbol="fUST",
    )


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class _AllowGuard:
    def __init__(self, name: str) -> None:
        self.name = name
        self.is_calibrated = False
        self.calls = 0

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        self.calls += 1
        return GuardResult(allowed=True, guard_name=self.name)


class _BlockGuard:
    def __init__(self, name: str, reason: str = "blocked") -> None:
        self.name = name
        self.is_calibrated = False
        self.calls = 0
        self._reason = reason

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        self.calls += 1
        return GuardResult(allowed=False, guard_name=self.name, reason=self._reason)


class _CrashGuard:
    name = "crash"
    is_calibrated = False

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        raise RuntimeError("boom")


class _HangGuard:
    name = "hang"
    is_calibrated = False

    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        await asyncio.sleep(GUARD_EVAL_TIMEOUT_SECONDS + 5)
        return GuardResult(allowed=True, guard_name=self.name)


def _chain(guards: list[Any], sink: _EventCapture, probe: HealthProbe) -> SafetyGuardChain:
    return SafetyGuardChain(
        guards=guards, probe=probe, diagnostics=sink,
        phase=Phase.LIVE, strategy=StrategyName.MEAN_REVERSION,
        cell="c1", account_id="default",
    )


@pytest.mark.asyncio
async def test_all_allow_reports_would_submit_true() -> None:
    guards = [_AllowGuard("a"), _AllowGuard("b")]
    report = await _chain(guards, _EventCapture(), HealthProbe()).dry_evaluate(
        _post(), _ctx(),
    )
    assert report.would_submit is True
    assert report.blocked_by is None
    assert [(g.name, g.allowed) for g in report.guards] == [("a", True), ("b", True)]


@pytest.mark.asyncio
async def test_block_reports_would_submit_false_and_names_the_blocker() -> None:
    guards = [_AllowGuard("a"), _BlockGuard("manual_kill", "trading state HALTED: operator stop")]
    report = await _chain(guards, _EventCapture(), HealthProbe()).dry_evaluate(
        _post(), _ctx(),
    )
    assert report.would_submit is False
    assert report.blocked_by == "manual_kill"
    blocked = [g for g in report.guards if not g.allowed]
    assert blocked[0].reason == "trading state HALTED: operator stop"


@pytest.mark.asyncio
async def test_evaluates_every_guard_past_the_first_block() -> None:
    """The real path short-circuits; the probe does not. Seeing that guard #3
    would ALSO block is what tells an operator resuming is not one env var away."""
    later = _BlockGuard("buying_power")
    guards = [_AllowGuard("a"), _BlockGuard("manual_kill"), later]
    report = await _chain(guards, _EventCapture(), HealthProbe()).dry_evaluate(
        _post(), _ctx(),
    )
    assert later.calls == 1
    assert [g.name for g in report.guards] == ["a", "manual_kill", "buying_power"]
    assert report.blocked_by == "manual_kill"  # still the FIRST one
    assert [g.name for g in report.guards if not g.allowed] == [
        "manual_kill", "buying_power",
    ]


@pytest.mark.asyncio
async def test_emits_nothing_and_records_no_heartbeat() -> None:
    """A probe that emitted safety_trigger would pollute the very table the
    incident-response queries read; one that bumped the heartbeat would forge
    evidence the daemon was trading."""
    sink, probe = _EventCapture(), HealthProbe()
    await _chain([_BlockGuard("manual_kill")], sink, probe).dry_evaluate(
        _post(), _ctx(),
    )
    assert sink.events == []
    assert "safety_chain" not in probe.last_active_ts


@pytest.mark.asyncio
async def test_real_evaluate_still_emits_and_beats_after_a_dry_run() -> None:
    """Guard against a refactor that makes the real path share the probe's
    suppression flags."""
    sink, probe = _EventCapture(), HealthProbe()
    chain = _chain([_BlockGuard("manual_kill")], sink, probe)
    await chain.dry_evaluate(_post(), _ctx())
    result = await chain.evaluate(_post(), _ctx())
    assert result.allowed is False
    assert len(sink.events) == 1
    assert "safety_chain" in probe.last_active_ts


@pytest.mark.asyncio
async def test_guard_exception_reports_blocked_with_internal_error() -> None:
    report = await _chain(
        [_CrashGuard(), _AllowGuard("after")], _EventCapture(), HealthProbe(),
    ).dry_evaluate(_post(), _ctx())
    crash = report.guards[0]
    assert crash.allowed is False
    assert crash.internal_error is True
    assert "boom" in (crash.reason or "")
    assert report.would_submit is False
    assert report.guards[1].name == "after"  # probe continues past the crash


@pytest.mark.asyncio
async def test_guard_timeout_reports_blocked_without_hanging_the_probe() -> None:
    report = await _chain(
        [_HangGuard()], _EventCapture(), HealthProbe(),
    ).dry_evaluate(_post(), _ctx())
    assert report.would_submit is False
    assert report.guards[0].internal_error is True
    assert "timeout" in (report.guards[0].reason or "").lower()


@pytest.mark.asyncio
async def test_empty_chain_reports_would_submit_true() -> None:
    """No guards installed = nothing stops a submit. Must read as would_submit,
    not as a halt — "no guard blocked" and "no guard exists" are different
    states and the report must not conflate them."""
    report = await _chain([], _EventCapture(), HealthProbe()).dry_evaluate(
        _post(), _ctx(),
    )
    assert report.would_submit is True
    assert report.guards == []
