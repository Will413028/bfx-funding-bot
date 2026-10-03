"""Guards, durability (write-ahead, restart), concurrency and the between-requests tick."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.venue_observation import BitfinexVenueObservation
from bfx_funding_bot.modules.ledger import ObservationWindow, Scope
from bfx_funding_bot.modules.simulated_venue import (
    ALLOWED_REALMS,
    ConcurrentAppendError,
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
    InMemoryVenueEventStore,
    RealmRefusedError,
    SimAccount,
    SimulatedVenueConfig,
)
from bfx_funding_bot.modules.simulated_venue.events import VenueEvent
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue
from tests.modules.simulated_venue.helpers import (
    ACCOUNT,
    ACCOUNT_ID,
    API_KEY,
    API_SECRET,
    CTX,
    DAY,
    HOUR,
    T0,
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
            account=ACCOUNT, config=config(), store=InMemoryVenueEventStore(),
            feed=(await make_world()).feed, clock_ms=lambda: T0, authority_epoch=epoch)


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
    assert losing.status_code == 500  # ConcurrentAppendError surfaces as a venue failure
    assert len(await w.store.load(ACCOUNT)) == 3  # funded, book, offer: not overwritten


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


async def test_whole_second_offer_timestamps_knob() -> None:
    w = await make_world(funds={"UST": "1000"}, cfg=config(whole_second_offer_mts=True))
    w.clock.now = T0 + 1234
    oid = await w.submit_ok(amount="150")
    (row,) = (await w.post("v2/auth/r/funding/offers", {})).json()
    assert row[0] == oid and row[2] == T0 + 1000


async def test_submit_without_any_book_is_refused_by_the_venue() -> None:
    w = await make_world(funds={"UST": "1000"}, seed_book=False)
    response = await w.submit(amount="150")
    assert response.status_code == 500 and "book" in response.json()[2]
    w.feed.add_book(book("fUST", T0))
    assert await w.submit_ok(amount="150")


# -- the world moves between two requests -----------------------------------------

def _observer(w: World) -> BitfinexVenueObservation:
    scope = Scope(UUID(ACCOUNT_ID), "ci")
    ctx = AccountContext(ACCOUNT_ID, Credentials(API_KEY, API_SECRET), D(10000))
    return BitfinexVenueObservation(rest=w.rest, ctx=ctx, scope=scope, clock_ms=w.clock)


async def test_a_fill_between_the_first_and_confirmation_reads_makes_them_differ() -> None:
    w = await make_world(funds={"UST": "1000"})
    await w.submit_ok(amount="150")
    w.feed.add_trades("fUST", [trade(T0 + HOUR, "150")])
    observer = _observer(w)
    scope = Scope(UUID(ACCOUNT_ID), "ci")
    window = ObservationWindow(None, None, None)

    # Request order of one observation: wallets, offers, credits, loans, history..., then
    # the four active reads again. The tick lands right after the offers read.
    w.venue.tick_after_request(w.venue.requests + 2, lambda: w.clock.advance(HOUR))
    first, confirmation, _ = await observer.observe(scope, w.clock.now, window)
    assert first.offers != confirmation.offers  # first saw the offer resting
    assert len(first.offers) == 1 and confirmation.offers == ()
    assert len(first.credits) == len(confirmation.credits) == 1  # the loan came before read 4

    again_first, again_confirmation, _ = await observer.observe(scope, w.clock.now, window)
    assert (again_first.wallets, again_first.offers, again_first.credits) == (
        again_confirmation.wallets, again_confirmation.offers, again_confirmation.credits)


async def test_the_tick_hook_runs_once_after_the_nth_request_and_catches_up() -> None:
    w = await make_world(funds={"UST": "1000"})
    await w.submit_ok(amount="150")
    w.feed.add_trades("fUST", [trade(T0 + HOUR, "150")])
    calls: list[int] = []
    def hook() -> None:
        calls.append(w.venue.requests)
        w.clock.advance(HOUR)

    w.venue.tick_after_request(w.venue.requests + 1, hook)
    await w.rest.fetch_wallet_observations(ctx=CTX)
    assert calls == [w.venue.requests]
    # the venue caught up by itself, without another request
    assert w.venue.state.offers[next(iter(w.venue.state.offers))].status == "EXECUTED"
    await w.rest.fetch_wallet_observations(ctx=CTX)
    assert calls == [w.venue.requests - 1]  # not called again


async def test_concurrent_requests_are_serialized_and_see_consistent_state() -> None:
    w = await make_world(funds={"UST": "10000"})
    results = await asyncio.gather(*(w.submit(amount="150", rate=f"0.000{i + 1}")
                                     for i in range(8)))
    assert all(r.status_code == 200 for r in results)
    ids = [r.json()[4][0] for r in results]
    assert len(set(ids)) == 8
    assert w.venue.state.available("UST") == D("10000") - 8 * D("150")
