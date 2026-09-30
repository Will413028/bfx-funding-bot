"""Always-on pre-trade limits: the offer envelope, the throttle, config and policy schema 3.

The guard is exercised through its real ``evaluate`` (only the DB read is
stubbed; the SQL path is in tests/integration/test_pre_trade_limits.py) and,
where it matters, through the real ``SafetyGuardChain`` (cancel exemption).
Anything missing -- envelope, policy ceiling, market reference -- refuses.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel
from bfx_funding_bot.modules.execution.capital_repository import (
    CapitalBlockedError,
    policy_from_row,
)
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer, PriceBranch
from bfx_funding_bot.modules.execution.deployment.sizing import allocate_capital
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.config import load_safety_config
from bfx_funding_bot.modules.execution.safety.pre_trade import (
    CommandThrottle,
    OfferEnvelopeGuard,
    PreTradeConfigurationError,
    build_pre_trade_guards,
    reference_bid_rate,
    require_pre_trade_limits,
)
from bfx_funding_bot.modules.execution.safety.protection import COMMAND_RATE_EXCEEDED
from bfx_funding_bot.modules.ledger.tables import CapitalPolicyRevisionRow
from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.strategy import (
    DecisionOutcome,
    DecisionPayload,
    SkipReason,
    StrategyName,
)
from bfx_funding_bot.modules.trading import (
    CapitalPolicy,
    OfferEnvelope,
    policy_payload,
    policy_schema_version,
)

CONFIGS = Path(__file__).resolve().parents[4] / "configs"
CTX = AccountContext(str(uuid4()), Credentials("k", "s"), Decimal("0"))


def post(*, rate: float = 0.0002, amount: float = 200, period: int = 2,
         symbol: str = "fUST") -> DecisionPayload:
    return DecisionPayload(decision_outcome=DecisionOutcome.POST, signal_correlation_id=uuid4(),
                           offer_rate=rate, offer_amount_usdt=amount, offer_duration_days=period,
                           symbol=symbol)


def skip() -> DecisionPayload:
    return DecisionPayload(decision_outcome=DecisionOutcome.SKIP, signal_correlation_id=uuid4(),
                           skip_reason=next(iter(SkipReason)), symbol="fUST")


def book(*bids: tuple[float, int], symbol: str = "fUST", valid: bool = True) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="book", symbol=symbol,
        bids=tuple(FundingBookLevel(rate=r, period=p, count=1, amount=-1000) for r, p in bids),
        asks=(FundingBookLevel(rate=0.0004, period=2, count=1, amount=1000),),
        captured_at_ms=1_000, received_at_ms=1_000, source="ws",
        sequence_valid=valid, checksum_valid=True, sequence=1,
    )


class Book:
    def __init__(self, snapshot: MarketSnapshot | None) -> None:
        self.value = snapshot

    def snapshot(self, symbol: str, *, now_ms: int) -> MarketSnapshot | None:
        return self.value


ENVELOPE = OfferEnvelope(min_period_days=2, max_period_days=2, max_open_offers=6,
                         rate_floor_ratio=Decimal("0.5"), min_rate_apr=Decimal("0.01"))
POLICY = CapitalPolicy(enabled=True, max_offer_amount=Decimal("200"), envelope=ENVELOPE)
MARKET = book((0.0003, 2), (0.0001, 2), (0.0002, 2))       # median bid 0.0002


class Guard(OfferEnvelopeGuard):
    """The real guard with its one DB read replaced."""

    def __init__(self, *, policy: CapitalPolicy | Exception = POLICY, open_offers: int = 0,
                 market: MarketSnapshot | None = MARKET) -> None:
        super().__init__(runtime=SimpleNamespace(), book=Book(market),  # type: ignore[arg-type]
                         clock=lambda: 1_000)
        self.policy, self.open_offers = policy, open_offers

    async def _policy_and_open(self, session: Any, symbol: str) -> tuple[CapitalPolicy, int]:
        if isinstance(self.policy, Exception):
            raise self.policy
        return self.policy, self.open_offers


SESSION_CTX = replace(CTX, command_session=object())  # type: ignore[arg-type]


async def envelope(decision: DecisionPayload, **kwargs: Any) -> Any:
    return await Guard(**kwargs).evaluate(decision, SESSION_CTX)


@pytest.mark.asyncio
async def test_an_offer_inside_the_envelope_passes_and_a_skip_is_never_judged() -> None:
    assert (await envelope(post())).allowed
    assert (await envelope(skip(), policy=RuntimeError("never read"))).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "reason"), [
    (CapitalPolicy(enabled=True), "envelope_unset"),                          # schema 1
    (CapitalPolicy(enabled=True, max_offer_amount=Decimal("200")), "envelope_unset"),  # schema 2
    (RuntimeError("db down"), "policy_unavailable: db down"),
])
async def test_no_envelope_or_no_policy_refuses(policy: Any, reason: str) -> None:
    result = await envelope(post(), policy=policy)
    assert (result.allowed, result.reason) == (False, reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(("amount", "allowed"), [(200, True), (200.01, False)])
async def test_amount_ceiling(amount: float, allowed: bool) -> None:
    result = await envelope(post(amount=amount))
    assert result.allowed is allowed
    if not allowed:
        assert result.reason.startswith("offer_amount 200.01 > max_offer_amount 200")


@pytest.mark.asyncio
@pytest.mark.parametrize(("period", "allowed"), [(1, False), (2, True), (3, False), (120, False)])
async def test_period_bounds(period: int, allowed: bool) -> None:
    result = await envelope(post(period=period))
    assert result.allowed is allowed
    if not allowed:
        assert result.reason == f"period {period} outside 2..2 days"


@pytest.mark.asyncio
@pytest.mark.parametrize(("open_offers", "allowed"), [(5, True), (6, False)])
async def test_open_offer_limit(open_offers: int, allowed: bool) -> None:
    result = await envelope(post(), open_offers=open_offers)
    assert result.allowed is allowed
    if not allowed:
        assert result.reason == "open_offers 6 >= limit 6"


def test_reference_is_the_exact_period_median_else_the_whole_book() -> None:
    bids = book((0.0003, 2), (0.0001, 2), (0.0002, 2), (0.0009, 30)).bids
    assert reference_bid_rate(bids, period_days=2) == Decimal("0.0002")
    assert reference_bid_rate(bids, period_days=7) == Decimal("0.00025")
    assert reference_bid_rate((), period_days=2) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("rate", "allowed"), [(0.0001, True), (0.0000999, False), (0.0005, True)])
async def test_relative_floor_is_ratio_times_median_bid(rate: float, allowed: bool) -> None:
    result = await envelope(post(rate=rate))
    assert result.allowed is allowed
    if not allowed:
        assert result.reason.startswith("offer_rate 0.0000999 < floor 0.00010")


@pytest.mark.asyncio
async def test_absolute_floor_holds_when_the_whole_market_collapses() -> None:
    """A market-wide collapse drags the relative floor down with it; min_rate_apr
    (1% a year = 0.0000274 a day) does not move."""
    collapsed = book((0.00002, 2), (0.00002, 2))            # relative floor 0.00001
    below = await envelope(post(rate=0.000027), market=collapsed)
    assert not below.allowed and "absolute 0.0000273972" in below.reason
    assert (await envelope(post(rate=0.0000274), market=collapsed)).allowed


@pytest.mark.asyncio
async def test_floor_is_the_higher_of_absolute_and_relative() -> None:
    high_apr = replace(POLICY, envelope=replace(ENVELOPE, min_rate_apr=Decimal("0.2")))
    # relative floor 0.0001, absolute 0.2 / 365 = 0.000548; the absolute one wins
    result = await envelope(post(rate=0.0005), policy=high_apr)
    assert not result.allowed and "< floor 0.000547945" in result.reason
    assert (await envelope(post(rate=0.00055), policy=high_apr)).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(("market", "decision", "reason"), [
    (None, post(), "rate_reference_unavailable"),
    (book((0.0002, 2), valid=False), post(), "rate_reference_unavailable"),
    (book((0.0002, 2), symbol="fUSD"), post(), "rate_reference_unavailable"),
    (book(), post(), "rate_reference_unavailable: no bids"),
    (MARKET, post(rate=0.0), "offer_rate_invalid"),
    (MARKET, post(amount=0), "offer_amount_invalid"),
])
async def test_envelope_fails_closed(market: MarketSnapshot | None, decision: DecisionPayload,
                                     reason: str) -> None:
    result = await envelope(decision, market=market)
    assert not result.allowed and result.reason.startswith(reason)


@pytest.mark.asyncio
async def test_floor_catches_a_taker_priced_at_an_abnormally_low_signal() -> None:
    """Phase 0 gap: period_pricing's TAKER branch prices at the signal whenever a
    bid pays at least that much, so a broken, tiny signal would lend at it."""
    snapshot = book((0.00031, 2), (0.0003, 2), (0.00029, 2))
    candidate = post(rate=0.00001, amount=200)
    priced = PeriodPricer(max_down_pct=Decimal("0.15"), tick=Decimal("0.00000001")).price(
        candidate=candidate, snapshot=snapshot)
    assert priced.branch is PriceBranch.TAKER and priced.rate == Decimal("0.00001")
    result = await envelope(candidate.model_copy(update={"offer_rate": priced.rate}),
                            market=snapshot)
    assert not result.allowed and "floor 0.00015" in result.reason


# --------------------------------------------------------------------------- chain / cancel


class Refuse:
    def __init__(self, name: str) -> None:
        self.name = name

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> Any:
        from bfx_funding_bot.modules.execution.protocols import GuardResult
        return GuardResult(False, self.name, "refused")


@pytest.mark.asyncio
async def test_the_envelope_never_refuses_a_cancel() -> None:
    chain = SafetyGuardChain(
        guards=[Refuse("offer_envelope")], probe=HealthProbe(),  # type: ignore[misc]
        diagnostics=SimpleNamespace(emit=lambda event: _noop()), phase=Phase.LIVE,  # type: ignore[arg-type]
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=CTX.account_id)
    assert (await chain.evaluate_cancel(post(), CTX)).allowed
    assert not (await chain.evaluate_transport(post(), CTX)).allowed
    assert not (await chain.evaluate(post(), CTX)).allowed


async def _noop() -> None:
    return None


# --------------------------------------------------------------------------- throttle


class Trips:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def trip(self, trigger: str, detail: str) -> None:
        self.calls.append((trigger, detail))


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def alert_log() -> Any:
    sink = alerts.AlertSink(None, context="ci")
    previous = alerts.install(sink)
    yield sink
    alerts.install(previous)


def test_throttle_admits_a_burst_then_refills(alert_log: Any) -> None:
    clock = Clock()
    throttle = CommandThrottle(capacity=3, refill_per_second=0.5, trip_blocks=10,
                               trip_window_s=300, protection=None, clock=clock)
    assert [throttle.admit("submit") for _ in range(4)] == [True, True, True, False]
    clock.now += 2                                         # one token back
    assert throttle.admit("cancel") and not throttle.admit("cancel")
    assert alert_log.counts == {"log_only": 1}              # a refused submit is alerted; a cancel waits


def test_sustained_excess_trips_once_per_episode(alert_log: Any) -> None:
    clock = Clock()
    trips = Trips()
    throttle = CommandThrottle(capacity=1, refill_per_second=0.001, trip_blocks=3,
                               trip_window_s=300, protection=trips, clock=clock)
    assert throttle.admit("submit")
    for _ in range(5):
        clock.now += 10
        assert not throttle.admit("submit")
    assert len(trips.calls) == 1 and trips.calls[0][0] == COMMAND_RATE_EXCEEDED
    clock.now += 400                                        # window empties: a new episode
    throttle._tokens = 0.0
    for _ in range(5):
        clock.now += 1
        assert not throttle.admit("cancel")
    assert len(trips.calls) == 1                            # pulling exposure never trips (D3)
    for _ in range(3):
        clock.now += 1
        throttle.admit("submit")
    assert len(trips.calls) == 2


def test_isolated_refusals_below_the_threshold_do_not_trip(alert_log: Any) -> None:
    clock = Clock()
    trips = Trips()
    throttle = CommandThrottle(capacity=1, refill_per_second=0.001, trip_blocks=3,
                               trip_window_s=300, protection=trips, clock=clock)
    throttle.admit("submit")
    for _ in range(4):
        clock.now += 200                                    # spread beyond the window
        throttle._tokens = 0.0
        throttle.admit("submit")
    assert trips.calls == []


# --------------------------------------------------------------------------- config


def test_live_config_carries_the_throttle_and_the_live_writer_requires_it() -> None:
    live = load_safety_config(CONFIGS / "safety.live.yaml")
    limits = require_pre_trade_limits(live.pre_trade_limits)
    assert limits.command_rate.capacity == 12 and limits.command_rate.trip_blocks == 6
    with pytest.raises(PreTradeConfigurationError) as missing:
        require_pre_trade_limits(None)
    assert isinstance(missing.value, ConfigurationError)
    with pytest.raises(PreTradeConfigurationError, match="funding book"):
        build_pre_trade_guards(runtime=SimpleNamespace(), book=None,  # type: ignore[arg-type]
                               clock=lambda: 1)
    guards = build_pre_trade_guards(runtime=SimpleNamespace(),  # type: ignore[arg-type]
                                    book=Book(None), clock=lambda: 1)
    assert [guard.name for guard in guards] == ["offer_envelope"]


def test_per_symbol_terms_no_longer_load_from_yaml(tmp_path: Path) -> None:
    import yaml

    raw = yaml.safe_load((CONFIGS / "safety.live.yaml").read_text())
    raw["pre_trade_limits"]["symbols"] = {"fUST": {"min_period_days": 2}}
    path = tmp_path / "safety.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_safety_config(path)


# --------------------------------------------------------------------------- policy schemas


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("NaN"), 200])
def test_max_offer_amount_must_be_a_positive_decimal(value: Any) -> None:
    with pytest.raises(ValueError):
        CapitalPolicy(enabled=True, max_offer_amount=value)


def _row(policy: dict[str, Any], schema: int) -> CapitalPolicyRevisionRow:
    import hashlib
    import json

    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return CapitalPolicyRevisionRow(schema_version=schema, policy=policy, digest=digest)


def test_policy_schema_2_round_trips_and_schema_1_stays_readable_without_a_ceiling() -> None:
    capped = CapitalPolicy(enabled=True, max_offer_amount=Decimal("200"))
    assert policy_schema_version(capped) == 2
    assert policy_payload(capped)["max_offer_amount"] == "200"
    assert policy_from_row(_row(policy_payload(capped), 2)) == capped
    legacy = CapitalPolicy(enabled=True)
    assert policy_schema_version(legacy) == 1 and "max_offer_amount" not in policy_payload(legacy)
    assert policy_from_row(_row(policy_payload(legacy), 1)).max_offer_amount is None


@pytest.mark.parametrize(("payload", "schema"), [
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7"}, 2),                                  # v2 without the ceiling
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7", "max_offer_amount": "200"}, 1),       # v1 smuggling it in
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7", "max_offer_amount": "0"}, 2),         # zero ceiling
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7"}, 4),                                  # unknown schema
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7", "max_offer_amount": "200"}, 3),       # v3 without envelope
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7", "max_offer_amount": "200",
      "envelope": {"min_period_days": 2, "max_period_days": 2, "max_open_offers": 6,
                   "rate_floor_ratio": "0.5"}}, 3),                     # envelope missing a key
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7", "max_offer_amount": "200",
      "envelope": {"min_period_days": 3, "max_period_days": 2, "max_open_offers": 6,
                   "rate_floor_ratio": "0.5", "min_rate_apr": "0.01"}}, 3),  # min > max
    ({"enabled": True, "reserve_amount": "0", "allocation_mode": "all_available",
      "max_cell_fraction": "0.7", "max_offer_amount": "200",
      "envelope": {"min_period_days": True, "max_period_days": 2, "max_open_offers": 6,
                   "rate_floor_ratio": "0.5", "min_rate_apr": "0.01"}}, 3),  # bool is not int
])
def test_malformed_policy_rows_are_refused(payload: dict[str, Any], schema: int) -> None:
    with pytest.raises(CapitalBlockedError):
        policy_from_row(_row(payload, schema))


def test_policy_schema_3_round_trips_the_envelope() -> None:
    assert policy_schema_version(POLICY) == 3
    payload = policy_payload(POLICY)
    assert payload["envelope"] == {"min_period_days": 2, "max_period_days": 2,
                                   "max_open_offers": 6, "rate_floor_ratio": "0.5",
                                   "min_rate_apr": "0.01"}
    assert policy_from_row(_row(payload, 3)) == POLICY


@pytest.mark.parametrize(("change", "value"), [
    ("min_period_days", 1), ("max_period_days", 121), ("min_period_days", 3),
    ("max_open_offers", 0), ("rate_floor_ratio", Decimal("1.5")),
    ("rate_floor_ratio", Decimal("0")), ("min_rate_apr", Decimal("0")),
    ("min_rate_apr", Decimal("1")), ("max_open_offers", 6.0),
])
def test_invalid_envelopes_are_refused(change: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(ENVELOPE, **{change: value})


def test_an_envelope_requires_the_amount_ceiling() -> None:
    with pytest.raises(ValueError, match="requires max_offer_amount"):
        CapitalPolicy(enabled=True, envelope=ENVELOPE)


def _view(policy: CapitalPolicy, max_new_offer: str) -> Any:
    budget = SimpleNamespace(spendable=Decimal("1000"), max_new_offer=Decimal(max_new_offer))
    return SimpleNamespace(applied=SimpleNamespace(policy=policy), snapshot_seq=1, budget=budget,
                           snapshot=SimpleNamespace(cell_exposure=Decimal("0")))


def test_sizing_stays_within_the_per_offer_ceiling() -> None:
    capped = CapitalPolicy(enabled=True, max_offer_amount=Decimal("200"))
    view = _view(capped, "700")
    assert allocate_capital(views={"a30": view}, min_fill=Decimal("150")) == {"a30": Decimal("200")}
    uncapped = _view(CapitalPolicy(enabled=True), "700")
    assert allocate_capital(views={"a30": uncapped}, min_fill=Decimal("150")) == {"a30": Decimal("700")}
