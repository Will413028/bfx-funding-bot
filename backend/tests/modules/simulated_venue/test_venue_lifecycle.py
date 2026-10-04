"""Guards, durability (write-ahead, restart), concurrency and the between-requests tick."""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.simulated_venue import (
    ALLOWED_REALMS,
    ConcurrentAppendError,
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
    FeedFailureError,
    FixtureMarketFeed,
    InMemoryVenueEventStore,
    NoMarketDataError,
    RealmRefusedError,
    SimAccount,
    SimulatedVenueConfig,
    SimulatedVenueInternalError,
)
from bfx_funding_bot.modules.simulated_venue.events import VenueEvent
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue
from tests.modules.simulated_venue.helpers import (
    ACCOUNT,
    CTX,
    DAY,
    HOUR,
    T0,
    Clock,
    World,
    book,
    config,
    make_world,
    trade,
)

D = Decimal


# -- realm and epoch guards -------------------------------------------------

def test_realm_prod_and_unknown_realms_are_refused_at_construction() -> None:
    for realm in ALLOWED_REALMS:
        assert SimAccount("a", realm).deployment_environment == realm
    for realm in ("prod", "PROD", "paper", ""):
        with pytest.raises(RealmRefusedError):
            SimAccount("a", realm)
    with pytest.raises(ValueError):
        SimAccount("", "ci")


@pytest.mark.parametrize("epoch", ["legacy", "", "Ledger", "unknown"])
async def test_wiring_refuses_any_authority_epoch_but_ledger(epoch: str) -> None:
    with pytest.raises(RealmRefusedError):
        await build_simulated_venue(
            account=ACCOUNT, config=config(), store=InMemoryVenueEventStore(authority_epoch=epoch),
            feed=(await make_world()).feed, clock_ms=lambda: T0)


def test_wiring_takes_no_epoch_from_its_caller() -> None:
    # The store reports the epoch it read itself; a caller-supplied value is not an option.
    assert "authority_epoch" not in inspect.signature(build_simulated_venue).parameters


def test_config_refuses_missing_synthetic_credentials_and_nonsense() -> None:
    for kwargs in ({"api_key": "", "api_secret": "s"}, {"api_key": "k", "api_secret": ""}):
        with pytest.raises(ValueError):
            SimulatedVenueConfig(**kwargs)
    for bad in ({"symbols": ()}, {"symbols": ("UST",)}, {"fee_rate": D("1")},
                {"history_max_limit": 501}, {"history_lag_ms": -1}, {"draw_after_ms": -1}):
        with pytest.raises(ValueError):
            replace(config(), **bad)
    assert config().business_rejection == "5xx" and config().history_lag_ms == 0


# -- durability ---------------------------------------------------------------

async def _scenario(w: World) -> None:
    first = await w.submit_ok(amount="300", rate="0.0002")
    await w.submit_ok(amount="200", rate="0.0009")
    w.feed.add_trades("fUST", [trade(T0 + HOUR, "500")])
    w.clock.advance(HOUR)
    await w.cancel(first)
    w.clock.advance(3 * DAY)


async def _snapshot(w: World) -> list[tuple[int, bytes]]:
    paths = [
        ("v2/auth/r/wallets", {}), ("v2/auth/r/funding/offers", {}),
        ("v2/auth/r/funding/credits", {}), ("v2/auth/r/funding/loans", {}),
        *((f"v2/auth/r/funding/{s}/fUST/hist", {"start": 0, "end": T0 + 9 * DAY, "limit": 50})
          for s in ("offers", "credits", "loans", "trades")),
        ("v2/auth/r/ledgers/UST/hist", {"category": 28, "start": 0, "end": T0 + 9 * DAY}),
    ]
    out = []
    for path, body in paths:
        response = await w.post(path, body)
        out.append((response.status_code, response.content))
    return out


async def test_a_restarted_venue_over_the_same_store_answers_byte_identically() -> None:
    w = await make_world(funds={"UST": "1000"})
    await _scenario(w)
    before = await _snapshot(w)
    assert any(len(content) > 40 for _, content in before)
    reborn = await make_world(store=w.store, feed=w.feed, clock=w.clock)  # no re-funding
    assert reborn.venue.state == w.venue.state
    assert await _snapshot(reborn) == before


async def test_state_after_replay_equals_the_live_state_after_every_step() -> None:
    w = await make_world(funds={"UST": "1000"})
    for step in range(6):
        if step % 2 == 0:
            await w.submit_ok(amount="150", rate=f"0.000{step + 1}")
        w.feed.add_trades("fUST", [trade(w.clock.now + HOUR, "120")])
        w.clock.advance(HOUR + 7 * 60_000)
        await w.rest.fetch_wallet_observations(ctx=CTX)
        reborn = await make_world(store=w.store, feed=w.feed, clock=w.clock)
        assert reborn.venue.state == w.venue.state


class FlakyStore(InMemoryVenueEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    async def append(self, account: SimAccount, expected_seq: int,
                     events: Sequence[VenueEvent]) -> None:
        if self.fail_next:
            self.fail_next = False
            raise OSError("disk full")
        await super().append(account, expected_seq, events)


async def test_the_response_is_never_produced_before_the_append_is_durable() -> None:
    store = FlakyStore()
    w = await make_world(funds={"UST": "1000"}, store=store)
    store.fail_next = True
    response = await w.submit(amount="150")
    assert response.status_code == 500 and response.json()[0] == "error"  # UNKNOWN to the client
    assert [f.kind for f in w.venue.internal_failures] == ["store"]  # counted apart from faults
    w.venue.internal_failures.clear()
    assert not w.venue.state.offers  # no phantom offer in memory ...
    reborn = await make_world(store=store, feed=w.feed, clock=w.clock)
    assert not reborn.venue.state.offers  # ... and none after a restart
    assert await w.submit_ok(amount="150")  # the venue recovered; nothing was half-applied


async def test_a_lost_response_still_leaves_the_offer_after_restart() -> None:
    plan = FaultPlan(rules=(FaultRule(
        FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST, ordinals=frozenset({1})),))
    w = await make_world(funds={"UST": "1000"}, faults=plan)
    with pytest.raises(Exception):  # noqa: B017 - the transport error type is asserted elsewhere
        await w.submit(amount="150")
    reborn = await make_world(store=w.store, feed=w.feed, clock=w.clock)
    assert len(reborn.venue.state.offers) == 1


async def test_a_second_writer_on_the_same_scope_fails_instead_of_overwriting() -> None:
    w = await make_world(funds={"UST": "1000"})
    other = await make_world(store=w.store, feed=w.feed, clock=w.clock)  # stale view of the log
    await w.submit_ok(amount="150")
    losing = await other.submit(amount="150")
    assert losing.status_code == 500  # the write was not recorded: caller unsure
    assert [f.kind for f in other.venue.internal_failures] == ["store"]
    other.venue.internal_failures.clear()
    log = await w.store.load(ACCOUNT)
    recorded = [e for e in log if type(e).__name__ == "InternalFailureRecorded"]
    assert [e.kind for e in recorded] == ["store"]  # the loser keeps a durable copy of it
    noise = {"NonceAdvanced", "InternalFailureRecorded"}
    assert len([e for e in log if type(e).__name__ not in noise]) == 3
    # the loser reloaded the durable log, so it converges instead of staying stale forever
    assert other.venue.state == w.venue.state
    assert await other.submit_ok(amount="150")


async def test_memory_store_enforces_expected_seq_and_isolates_scopes() -> None:
    store = InMemoryVenueEventStore()
    a, b = SimAccount("a", "ci"), SimAccount("a", "shadow")
    from bfx_funding_bot.modules.simulated_venue.events import WalletFunded
    event = WalletFunded("UST", D("1"), T0)
    await store.append(a, 0, [event])
    with pytest.raises(ConcurrentAppendError):
        await store.append(a, 0, [event])
    with pytest.raises(ConcurrentAppendError):
        await store.append(a, 2, [event])
    await store.append(b, 0, [event, event])
    assert len(await store.load(a)) == 1 and len(await store.load(b)) == 2
    assert await store.load(SimAccount("zzz", "ci")) == ()


# -- clock ---------------------------------------------------------------------

async def test_the_venue_stamps_from_the_injected_clock_never_the_wall_clock() -> None:
    w = await make_world(funds={"UST": "1000"})
    oid = await w.submit_ok(amount="150")
    row = (await w.post("v2/auth/r/funding/offers", {})).json()[0]
    assert row[0] == oid and row[2] == row[3] == T0  # far from the wall clock
    w.clock.advance(HOUR)
    assert (await w.cancel(oid)).json()[0] == T0 + HOUR


async def test_a_clock_that_steps_back_never_moves_venue_time_back() -> None:
    w = await make_world(funds={"UST": "1000"})
    w.clock.advance(DAY)
    await w.submit_ok(amount="150")
    w.clock.now = T0  # a test or host bug
    oid2 = await w.submit_ok(amount="150")
    created = {r[0]: r[2] for r in (await w.post("v2/auth/r/funding/offers", {})).json()}
    assert created[oid2] >= T0 + DAY


async def test_whole_second_offer_timestamps_are_the_default_and_the_knob_turns_them_off() -> None:
    assert config().whole_second_offer_mts is True
    w = await make_world(funds={"UST": "1000"}, cfg=config(whole_second_offer_mts=False))
    w.clock.now = T0 + 1234
    await w.submit_ok(amount="150")
    (row,) = (await w.post("v2/auth/r/funding/offers", {})).json()
    assert row[2] == T0 + 1234


async def test_submit_without_any_book_is_no_market_data_not_a_venue_answer() -> None:
    w = await make_world(funds={"UST": "1000"}, seed_book=False)
    with pytest.raises(NoMarketDataError):
        await _raw_submit(w)  # an exception, not a 5xx and not a rejection
    assert [f.kind for f in w.venue.internal_failures] == ["no_market_data"]
    w.venue.internal_failures.clear()
    assert not w.venue.state.offers
    w.feed.add_book(book("fUST", T0))
    assert await w.submit_ok(amount="150")


async def test_a_book_older_than_max_book_age_is_no_market_data_through_the_transport() -> None:
    w = await make_world(funds={"UST": "1000"}, cfg=config(max_book_age_ms=60_000),
                         seed_book=False)
    w.feed.add_book(book("fUST", T0))
    w.clock.advance(60_001)
    with pytest.raises(NoMarketDataError):
        await _raw_submit(w)
    assert [f.kind for f in w.venue.internal_failures] == ["no_market_data"]
    w.venue.internal_failures.clear()


async def _raw_submit(w: World) -> object:
    body = {"type": "LIMIT", "symbol": "fUST", "amount": "150", "rate": "0.0002",
            "period": 2, "flags": 0}
    return await w.post("v2/auth/w/funding/offer/submit", body)  # no book refresh


class SlowFeed(FixtureMarketFeed):
    async def book(self, symbol: str, *, at_ms: int):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)  # stands in for network I/O inside the venue lock
        return await super().book(symbol, at_ms=at_ms)


class BrokenFeed(FixtureMarketFeed):
    async def trades(self, symbol: str, *, after_ms: int, through_ms: int):  # type: ignore[no-untyped-def]
        raise RuntimeError("feed down")


async def test_a_feed_that_blocks_inside_the_lock_is_an_internal_failure() -> None:
    feed = SlowFeed()
    feed.add_book(book("fUST", T0))
    w = await make_world(funds={"UST": "1000"}, feed=feed, cfg=config(feed_deadline_s=0.05),
                         seed_book=False)
    with pytest.raises(FeedFailureError, match="network I/O"):
        await _raw_submit(w)
    assert [f.kind for f in w.venue.internal_failures] == ["feed"]
    w.venue.internal_failures.clear()


async def test_a_failing_feed_is_an_internal_failure_never_a_venue_5xx() -> None:
    w = await make_world(funds={"UST": "1000"}, feed=BrokenFeed())
    await w.submit_ok(amount="150")  # resting offer: the next request pulls trades
    w.clock.advance(1_000)
    with pytest.raises(FeedFailureError):
        await w.rest.fetch_wallet_observations(ctx=CTX)
    assert {f.kind for f in w.venue.internal_failures} == {"feed"}
    w.venue.internal_failures.clear()


async def test_a_simulator_bug_is_recorded_and_raised_not_answered_as_a_venue_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bfx_funding_bot.modules.simulated_venue._internal import decide as decide_module

    w = await make_world(funds={"UST": "1000"})

    def boom(*args: object, **kwargs: object) -> None:
        raise KeyError("fold bug")

    monkeypatch.setattr(decide_module, "decide_submit", boom)
    with pytest.raises(SimulatedVenueInternalError, match="simulator bug"):
        await w.submit(amount="150")
    assert [f.kind for f in w.venue.internal_failures] == ["bug"]
    w.venue.internal_failures.clear()


# -- the world moves between two requests -----------------------------------------

def _tick_plan(n: int, hook: Callable[[], None]) -> FaultPlan:
    return FaultPlan(rules=(FaultRule(
        FaultTarget.ANY_REQUEST, FaultKind.TICK_AFTER, ordinals=frozenset({n}), hook=hook),))


async def test_the_tick_rule_runs_once_after_the_nth_request_and_catches_up() -> None:
    calls: list[int] = []
    holder: list[World] = []

    def hook() -> None:
        calls.append(holder[0].venue.requests)
        holder[0].clock.advance(HOUR)

    w = await make_world(funds={"UST": "1000"}, faults=_tick_plan(2, hook))
    holder.append(w)
    await w.submit_ok(amount="150")  # request 1
    w.feed.add_trades("fUST", [trade(T0 + HOUR, "150")])
    await w.rest.fetch_wallet_observations(ctx=CTX)  # request 2: the rule fires after it
    assert calls == [2]
    # the venue caught up by itself, without another request
    assert next(iter(w.venue.state.offers.values())).status == "EXECUTED"
    await w.rest.fetch_wallet_observations(ctx=CTX)
    assert calls == [2]  # not again


async def test_concurrent_requests_are_serialized_and_see_consistent_state() -> None:
    w = await make_world(funds={"UST": "10000"})
    results = await asyncio.gather(*(w.submit(amount="150", rate=f"0.000{i + 1}")
                                     for i in range(8)))
    assert all(r.status_code == 200 for r in results)
    ids = [r.json()[4][0] for r in results]
    assert len(set(ids)) == 8
    assert w.venue.state.available("UST") == D("10000") - 8 * D("150")


async def test_wallets_are_funded_in_one_append_and_only_on_an_empty_log() -> None:
    """Mutation: fund on every boot (drop the empty-log check)."""
    store = InMemoryVenueEventStore()
    clock = Clock()
    funds = {"UST": Decimal(1000), "USD": Decimal(50)}
    first = await build_simulated_venue(
        account=ACCOUNT, config=config(), store=store, feed=FixtureMarketFeed(), clock_ms=clock)
    assert await first.fund_wallets_if_empty(funds) is True
    assert len(await store.load(ACCOUNT)) == 2  # one append, both currencies
    # A restart over the same log: nothing is funded again.
    second = await build_simulated_venue(
        account=ACCOUNT, config=config(), store=store, feed=FixtureMarketFeed(), clock_ms=clock)
    assert await second.fund_wallets_if_empty(funds) is False
    assert (second.state.wallets["UST"].balance, second.state.wallets["USD"].balance) == (
        Decimal(1000), Decimal(50))
    assert len(await store.load(ACCOUNT)) == 2
    assert await first.fund_wallets_if_empty({}) is False


async def test_two_processes_funding_an_empty_log_lose_the_race_loudly() -> None:
    store = InMemoryVenueEventStore()
    clock = Clock()
    venues = [
        await build_simulated_venue(
            account=ACCOUNT, config=config(), store=store, feed=FixtureMarketFeed(), clock_ms=clock)
        for _ in range(2)
    ]
    await venues[0].fund_wallets_if_empty({"UST": Decimal(1000)})
    with pytest.raises(ConcurrentAppendError):
        await venues[1].fund_wallets_if_empty({"UST": Decimal(1000)})
    assert len(await store.load(ACCOUNT)) == 1


class _Counting:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.unexpected = 0

    def internal_failure(self, kind: str) -> None:
        self.failures.append(kind)

    def unexpected_request(self) -> None:
        self.unexpected += 1


async def test_internal_failures_and_unrouted_requests_reach_the_observer_and_the_totals() -> None:
    """Mutation: the venue records only in memory (no observer call, no total)."""
    observer = _Counting()
    store = InMemoryVenueEventStore()
    venue = await build_simulated_venue(
        account=ACCOUNT, config=config(), store=store, feed=FixtureMarketFeed(),
        clock_ms=Clock(), observer=observer)
    async with venue.client() as http:
        assert (await http.get("https://api-pub.bitfinex.com/v2/anything")).status_code == 404
    await venue.fund_wallet("UST", Decimal(1000))
    with pytest.raises(NoMarketDataError):
        await _raw_submit(World(venue, FixtureMarketFeed(), Clock(), store))
    assert observer.unexpected == 1 and venue.unexpected_total == 1
    assert observer.failures == ["no_market_data"] and venue.internal_failures_total == 1
    venue.internal_failures.clear()
    venue.unexpected.clear()
    # ... and both are in the durable log too, so a restart or a crash cannot lose them
    from bfx_funding_bot.modules.simulated_venue.events import (
        InternalFailureRecorded,
        UnexpectedRequestRecorded,
    )
    log = await store.load(ACCOUNT)
    assert [(e.method, e.url) for e in log if isinstance(e, UnexpectedRequestRecorded)] == [
        ("GET", "https://api-pub.bitfinex.com/v2/anything")]
    assert [e.kind for e in log if isinstance(e, InternalFailureRecorded)] == ["no_market_data"]
    reopened = await build_simulated_venue(
        account=ACCOUNT, config=config(), store=store, feed=FixtureMarketFeed(), clock_ms=Clock())
    assert reopened.state.wallets["UST"].balance == Decimal(1000)  # the records change nothing


async def test_durable_failure_records_are_bounded_and_never_raise() -> None:
    from bfx_funding_bot.modules.simulated_venue._internal.transport import PERSIST_LIMIT
    from bfx_funding_bot.modules.simulated_venue.events import (
        DETAIL_LIMIT,
        UnexpectedRequestRecorded,
    )

    store = InMemoryVenueEventStore()
    venue = await build_simulated_venue(
        account=ACCOUNT, config=config(), store=store, feed=FixtureMarketFeed(), clock_ms=Clock())
    async with venue.client() as http:
        for _ in range(PERSIST_LIMIT + 7):
            await http.get("https://api-pub.bitfinex.com/v2/" + "x" * 1_000)
    rows = [e for e in await store.load(ACCOUNT) if isinstance(e, UnexpectedRequestRecorded)]
    assert len(rows) == PERSIST_LIMIT and venue.unexpected_total == PERSIST_LIMIT + 7
    assert all(len(e.url) == DETAIL_LIMIT for e in rows)
    venue.unexpected.clear()

    class _Broken(InMemoryVenueEventStore):
        async def append(self, account, expected_seq, events):  # type: ignore[no-untyped-def]
            raise RuntimeError("disk gone")

    broken = await build_simulated_venue(
        account=ACCOUNT, config=config(), store=_Broken(), feed=FixtureMarketFeed(),
        clock_ms=Clock())
    async with broken.client() as http:
        assert (await http.get("https://api-pub.bitfinex.com/v2/anything")).status_code == 404
    assert broken.unexpected_total == 1  # counted in memory although it could not be kept
    broken.unexpected.clear()


async def test_the_in_memory_logs_are_bounded_but_the_totals_are_not() -> None:
    from bfx_funding_bot.modules.simulated_venue._internal.transport import LOG_LIMIT

    venue = await build_simulated_venue(
        account=ACCOUNT, config=config(), store=InMemoryVenueEventStore(),
        feed=FixtureMarketFeed(), clock_ms=Clock())
    async with venue.client() as http:
        for _ in range(LOG_LIMIT + 5):
            await http.get("https://api-pub.bitfinex.com/v2/anything")
    assert len(venue.unexpected) == LOG_LIMIT and venue.unexpected_total == LOG_LIMIT + 5
    assert venue.unexpected == list(venue.unexpected) and venue.unexpected != []
    venue.unexpected.clear()
