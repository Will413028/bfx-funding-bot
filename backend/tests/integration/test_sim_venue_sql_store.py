"""The durable venue store on clone PostgreSQL: guards, optimistic concurrency, restart.

The store runs under ``bfx_bot`` (``server_settings role``) like the simulation bot would, so
the grants of the migration are what the tests exercise.

Mutations (one at a time, revert after each, run this file):

* ``authority_epoch`` / ``open`` skip the epoch read (return ``ledger``):
  ``test_open_refuses_a_database_whose_latest_epoch_is_not_ledger``.
* ``open`` accepts a caller-supplied epoch (parameter added, used instead of the read):
  ``test_the_store_takes_no_epoch_from_its_caller`` and the legacy refusal test.
* ``append`` uses the database maximum instead of ``expected_seq``:
  ``test_a_stale_writer_loses`` and ``test_concurrent_writers_exactly_one_wins``.
* ``load`` ignores the scope filter: ``test_scopes_are_disjoint``.
* ``load`` orders by nothing / reverses: ``test_load_returns_events_in_seq_order``.
* ``_check_database_realm`` accepts a ``prod`` stamp / an unstamped database / is skipped:
  the three ``test_open_refuses_*`` realm tests.
* The decoder drops ``NonceAdvanced`` from ``_EVENTS``: the restart tests fail.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.modules.simulated_venue import (
    ConcurrentAppendError,
    RealmRefusedError,
    SimAccount,
    SqlVenueEventStore,
    VenueStoreError,
)
from bfx_funding_bot.modules.simulated_venue.events import (
    SCHEMA_VERSION,
    BookObserved,
    CreditClosed,
    FaultInjected,
    InterestPaid,
    InternalFailureRecorded,
    LoanDrawn,
    NonceAdvanced,
    OfferCanceled,
    OfferFilled,
    OfferPlaced,
    TradesObserved,
    TradeTick,
    UnexpectedRequestRecorded,
    VenueEvent,
    WalletFunded,
)
from tests.modules.simulated_venue.helpers import (
    ACCOUNT,
    API_KEY,
    API_SECRET,
    HOUR,
    T0,
    Clock,
    book,
    make_world,
    trade,
)
from tests.pg_templates import alembic, disable_realm_triggers, stamp_realm

from .test_ledger_schema_roles import pre_switch
from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

D = Decimal
OTHER = SimAccount("aaaaaaaa-bbbb-4ccc-8ddd-000000000002", "ci")
SHADOW = SimAccount(ACCOUNT.exchange_account_id, "shadow")


def _build_unstamped(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    engine.dispose()
    alembic(url, "upgrade", "head")


def _build_migrated(url: str) -> None:
    _build_unstamped(url)
    stamp_realm(url, "ci")


def _set_epoch(url: str, authority: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
            f"VALUES ((SELECT max(epoch_seq) + 1 FROM capital_authority_epoch), '{authority}', "
            "1, 'test', 'sim venue store')")
    engine.dispose()


def _engine(url: str, *, role: str | None = "bfx_bot") -> AsyncEngine:
    kwargs: dict[str, Any] = {}
    if role is not None:
        kwargs["connect_args"] = {"server_settings": {"role": role}}
    return create_async_engine(url.replace("+psycopg", "+asyncpg"), **kwargs)


@pytest.fixture
def legacy_url(pg_templates: Any, pg_clone: Any) -> str:
    """A head database before the switch (the genesis epoch taken back)."""
    url = pg_clone(pg_templates.template("sim_store_migrated", _build_migrated))
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            pre_switch(conn)
    finally:
        engine.dispose()
    return url


@pytest.fixture
def ledger_url(legacy_url: str) -> str:
    _set_epoch(legacy_url, "ledger")
    return legacy_url


@pytest_asyncio.fixture
async def engine(ledger_url: str) -> AsyncIterator[AsyncEngine]:
    engine = _engine(ledger_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> SqlVenueEventStore:
    return await SqlVenueEventStore.open(engine)


def _funded(n: int) -> list[VenueEvent]:
    return [WalletFunded("UST", D(n), T0 + n)]


# -- guards -------------------------------------------------------------------------

def _database_stamped(pg_templates: Any, pg_clone: Any, realm: str | None) -> str:
    url = pg_clone(pg_templates.template("sim_store_unstamped", _build_unstamped))
    if realm is not None:
        stamp_realm(url, realm)
    _set_epoch(url, "ledger")
    return url


async def test_open_refuses_an_unstamped_database(pg_templates: Any, pg_clone: Any) -> None:
    engine = _engine(_database_stamped(pg_templates, pg_clone, None))
    try:
        with pytest.raises(RealmRefusedError, match="not stamped"):
            await SqlVenueEventStore.open(engine)
    finally:
        await engine.dispose()


async def test_open_refuses_a_database_stamped_prod(pg_templates: Any, pg_clone: Any) -> None:
    engine = _engine(_database_stamped(pg_templates, pg_clone, "prod"))
    try:
        with pytest.raises(RealmRefusedError, match="stamped 'prod'"):
            await SqlVenueEventStore.open(engine)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("realm", ["shadow", "ci"])
async def test_open_accepts_a_simulation_realm_and_the_trigger_binds_the_account(
    pg_templates: Any, pg_clone: Any, realm: str
) -> None:
    engine = _engine(_database_stamped(pg_templates, pg_clone, realm))
    try:
        store = await SqlVenueEventStore.open(engine)
        own = SimAccount(ACCOUNT.exchange_account_id, realm)
        foreign = SimAccount(ACCOUNT.exchange_account_id, "ci" if realm == "shadow" else "shadow")
        await store.append(own, 0, _funded(1))
        with pytest.raises(VenueStoreError, match="refuses a write of realm"):
            await store.append(foreign, 0, _funded(2))
        assert len(await store.load(own)) == 1
        assert await store.load(foreign) == ()
    finally:
        await engine.dispose()


async def test_open_refuses_a_database_without_the_realm_table(ledger_url: str) -> None:
    alembic(ledger_url, "downgrade", "e6b1d4a7c9f3")
    engine = _engine(ledger_url)
    try:
        with pytest.raises(RealmRefusedError, match="database_realm"):
            await SqlVenueEventStore.open(engine)
    finally:
        await engine.dispose()


async def test_open_refuses_a_database_whose_latest_epoch_is_not_ledger(legacy_url: str) -> None:
    engine = _engine(legacy_url)
    try:
        with pytest.raises(RealmRefusedError, match="legacy"):
            await SqlVenueEventStore.open(engine)
    finally:
        await engine.dispose()


async def test_open_follows_the_latest_epoch_row_not_the_first(legacy_url: str) -> None:
    _set_epoch(legacy_url, "ledger")
    engine = _engine(legacy_url)
    try:
        store = await SqlVenueEventStore.open(engine)
        assert await store.authority_epoch() == "ledger"
    finally:
        await engine.dispose()


async def test_open_refuses_a_database_without_an_epoch_table(pg_templates: Any, pg_clone: Any) -> None:
    engine = _engine(pg_clone(pg_templates.template("sim_store_empty", lambda url: None)), role=None)
    try:
        with pytest.raises(RealmRefusedError, match="missing"):
            await SqlVenueEventStore.open(engine)
    finally:
        await engine.dispose()


async def test_open_refuses_a_database_without_the_event_table(ledger_url: str) -> None:
    alembic(ledger_url, "downgrade", "a3b4c5d6e7f8")
    engine = _engine(ledger_url)
    try:
        with pytest.raises(RealmRefusedError, match="sim_venue_event"):
            await SqlVenueEventStore.open(engine)
    finally:
        await engine.dispose()


def test_the_store_takes_no_epoch_from_its_caller() -> None:
    assert list(inspect.signature(SqlVenueEventStore.open).parameters) == ["engine"]
    assert list(inspect.signature(SqlVenueEventStore.__init__).parameters) == ["self", "engine"]


async def test_the_bot_role_reads_the_epoch_and_the_store_never_updates(engine: AsyncEngine) -> None:
    # `open` ran as bfx_bot (its SELECT on the epoch table); a store write path that needed
    # UPDATE or DELETE would be denied by the grants, so the whole suite is a grants test.
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT current_user")) == "bfx_bot"


# -- the log --------------------------------------------------------------------------

def _every_event_type() -> list[VenueEvent]:
    return [
        WalletFunded("UST", D("1000.5"), T0),
        BookObserved("fUST", T0, ((D("0.0002"), 2, D("500")), (D("0.00021"), 30, D("10"))), T0),
        NonceAdvanced(1_700_000_000_000_001, T0),
        OfferPlaced(40_000_001, "fUST", D("150"), D("0.0002"), 2, T0, D("500")),
        TradesObserved("fUST", T0 + HOUR, (TradeTick(T0 + 1, D("12.5"), D("0.0002"), 2),), T0 + HOUR),
        OfferFilled(40_000_001, 50_000_001, 60_000_001, D("150"), D("0.0002"), 2, T0 + HOUR),
        LoanDrawn(60_000_001, 70_000_001, T0 + HOUR),
        OfferCanceled(40_000_002, T0 + HOUR),
        CreditClosed("credit", 70_000_001, T0 + 3 * HOUR, "expired"),
        InterestPaid(80_000_001, "UST", D("0.12345678"), D("1000.62345678"), T0 + 5 * HOUR),
        FaultInjected("unknown_placed_lost", "submit", 7, 1_700_000_000_000_002, "fUST", D("150"),
                      D("0.0002"), 2, T0 + 6 * HOUR),
        FaultInjected("history_error", "history", 8, 1_700_000_000_000_003, None, None, None, None,
                      T0 + 6 * HOUR),
        InternalFailureRecorded("feed", "feed trades failed", T0 + 6 * HOUR),
        UnexpectedRequestRecorded("GET", "https://example.test/x", T0 + 6 * HOUR),
    ]


async def test_every_event_type_round_trips_through_the_store(store: SqlVenueEventStore) -> None:
    events = _every_event_type()
    await store.append(ACCOUNT, 0, events)
    assert list(await store.load(ACCOUNT)) == events


async def test_load_returns_events_in_seq_order(store: SqlVenueEventStore, ledger_url: str) -> None:
    await store.append(ACCOUNT, 0, _funded(1))
    await store.append(ACCOUNT, 1, _funded(2) + _funded(3))
    await store.append(ACCOUNT, 3, _funded(4))
    assert [e.amount for e in await store.load(ACCOUNT)] == [D(1), D(2), D(3), D(4)]  # type: ignore[union-attr]
    sync = create_engine(ledger_url)
    with sync.connect() as conn:
        rows = conn.execute(text(
            "SELECT seq, event_type, schema_version, payload->>'event_type', "
            "(payload->>'schema_version')::int FROM sim_venue_event ORDER BY seq")).all()
    sync.dispose()
    assert [r[0] for r in rows] == [1, 2, 3, 4]
    assert all(r[1] == r[3] == "wallet_funded" and r[2] == r[4] == SCHEMA_VERSION for r in rows)


async def test_an_empty_scope_loads_nothing_and_an_empty_append_is_a_no_op(
        store: SqlVenueEventStore) -> None:
    assert await store.load(ACCOUNT) == ()
    await store.append(ACCOUNT, 0, [])
    assert await store.load(ACCOUNT) == ()
    with pytest.raises(ConcurrentAppendError):
        await store.append(ACCOUNT, 3, [])  # even an empty append states the log's length


async def test_a_stale_writer_loses(store: SqlVenueEventStore) -> None:
    await store.append(ACCOUNT, 0, _funded(1) + _funded(2))
    for stale in (0, 1):
        with pytest.raises(ConcurrentAppendError):
            await store.append(ACCOUNT, stale, _funded(9))
    assert len(await store.load(ACCOUNT)) == 2


async def test_a_writer_from_the_future_cannot_leave_a_gap(store: SqlVenueEventStore) -> None:
    await store.append(ACCOUNT, 0, _funded(1))
    with pytest.raises(ConcurrentAppendError):
        await store.append(ACCOUNT, 5, _funded(2))
    assert len(await store.load(ACCOUNT)) == 1
    await store.append(ACCOUNT, 1, _funded(2))  # the log is still appendable at its true end


async def test_scopes_are_disjoint(store: SqlVenueEventStore) -> None:
    await store.append(ACCOUNT, 0, _funded(1))
    await store.append(OTHER, 0, _funded(2) + _funded(3))
    assert [e.amount for e in await store.load(ACCOUNT)] == [D(1)]  # type: ignore[union-attr]
    assert [e.amount for e in await store.load(OTHER)] == [D(2), D(3)]  # type: ignore[union-attr]
    assert await store.load(SHADOW) == ()  # the same account id in another realm is another scope
    with pytest.raises(ConcurrentAppendError):
        await store.append(ACCOUNT, 2, _funded(5))  # the other scopes' lengths are not its own


async def test_concurrent_writers_exactly_one_wins(store: SqlVenueEventStore) -> None:
    results = await asyncio.gather(
        *(store.append(ACCOUNT, 0, _funded(n)) for n in range(1, 9)), return_exceptions=True)
    losers = [r for r in results if isinstance(r, ConcurrentAppendError)]
    assert len(losers) == 7 and sum(r is None for r in results) == 1, results
    (winner,) = await store.load(ACCOUNT)
    assert isinstance(winner, WalletFunded)


async def test_concurrent_multi_event_writers_never_interleave(store: SqlVenueEventStore) -> None:
    batches = [[WalletFunded("UST", D(n), T0), WalletFunded("UST", D(n), T0 + 1)] for n in range(1, 6)]
    results = await asyncio.gather(
        *(store.append(ACCOUNT, 0, batch) for batch in batches), return_exceptions=True)
    assert sum(r is None for r in results) == 1
    loaded = await store.load(ACCOUNT)
    assert loaded in [tuple(b) for b in batches]  # one whole batch, in order


async def test_a_database_refusal_is_a_store_error_not_a_lost_race(
        store: SqlVenueEventStore, ledger_url: str) -> None:
    # Bypass SimAccount's own refusal: the database stamp is the next line of defence and the
    # realm CHECK the last (the trigger is switched off to reach it).
    class Forged:
        exchange_account_id = "forged"
        deployment_environment = "prod"

    with pytest.raises(VenueStoreError) as caught:
        await store.append(Forged(), 0, _funded(1))  # type: ignore[arg-type]
    assert not isinstance(caught.value, ConcurrentAppendError)
    assert "database realm ci refuses a write of realm prod" in str(caught.value)
    owner = create_engine(ledger_url)
    try:
        disable_realm_triggers(owner)
    finally:
        owner.dispose()
    with pytest.raises(VenueStoreError, match="ck_sim_venue_event_realm"):
        await store.append(Forged(), 0, _funded(1))  # type: ignore[arg-type]


# -- the venue over the store ----------------------------------------------------------

async def _reads(w: Any) -> list[Any]:
    paths = ("v2/auth/r/wallets", "v2/auth/r/funding/offers", "v2/auth/r/funding/credits",
             "v2/auth/r/funding/loans")
    out = [(await w.post(path, {})).json() for path in paths]
    for kind in ("offers", "credits", "loans", "trades"):
        response = await w.post(f"v2/auth/r/funding/{kind}/fUST/hist",
                                {"start": 0, "end": w.clock.now + 1, "limit": 500, "sort": -1})
        out.append(response.json())
    return out


async def test_a_restart_over_the_same_store_rebuilds_identical_state(store: SqlVenueEventStore) -> None:
    clock = Clock()
    w1 = await make_world(funds={"UST": "5000"}, store=store, clock=clock)  # type: ignore[arg-type]
    first = await w1.submit_ok(amount="150", rate="0.0002")
    second = await w1.submit_ok(amount="300", rate="0.00021")
    third = await w1.submit_ok(amount="200", rate="0.00022", period=30)
    assert (await w1.cancel(second)).status_code == 200
    w1.feed.add_trades("fUST", [trade(T0 + HOUR, "200")])
    clock.advance(HOUR + 60_000)
    await w1.rest.fetch_wallet_observations(ctx=_ctx())  # catch-up: fills, nonce advances
    reads_before = await _reads(w1)
    state = w1.venue.state  # the old process stops here; only one process ever writes the log
    assert state.offers[first].status == "EXECUTED"
    assert state.offers[second].status == "CANCELED" and state.offers[third].status == "ACTIVE"
    assert state.last_nonce > 0 and state.counters

    # A fresh process: new engine, new store object, new venue, same database.
    engine2 = _engine(str(store._engine.url.render_as_string(hide_password=False)).replace(
        "+asyncpg", "+psycopg"))
    try:
        store2 = await SqlVenueEventStore.open(engine2)
        w2 = await make_world(store=store2, clock=clock, seed_book=False)  # type: ignore[arg-type]
        w2.gate = w1.gate  # one nonce source per key, as one bot process restarting would not
        assert w2.venue.state == state  # offers, lendings, wallets, nonce high-water, id counters
        assert w2.venue.state.last_nonce == state.last_nonce
        assert w2.venue.state.counters == state.counters
        assert await _reads(w2) == reads_before  # answers JSON-identical after the restart

        # Ids keep counting up across the restart, in every kind's own space.
        w2.feed.add_book(book("fUST", clock.now))
        fourth = await w2.submit_ok(amount="150", rate="0.00023")
        assert fourth > max(first, second, third)
        for world in (w1, w2):
            assert world.venue.internal_failures == [] and world.venue.unexpected == []
    finally:
        await engine2.dispose()


def _ctx() -> Any:
    from tests.modules.simulated_venue.helpers import CTX
    return CTX


async def test_the_nonce_high_water_survives_a_restart(store: SqlVenueEventStore) -> None:
    w1 = await make_world(funds={"UST": "1000"}, store=store)  # type: ignore[arg-type]
    await w1.post("v2/auth/r/wallets", {})
    high = w1.venue.state.last_nonce
    assert high > 0
    w2 = await make_world(store=store, seed_book=False)  # type: ignore[arg-type]
    assert w2.venue.state.last_nonce == high
    body = b"{}"
    path = "v2/auth/r/wallets"
    for nonce, ok in ((high, False), (high - 1, False), (high + 1, True)):
        headers = sign_request(body=body, nonce=str(nonce), api_secret=API_SECRET, path=path)
        headers.update({"bfx-apikey": API_KEY, "Content-Type": "application/json"})
        response = await w2.venue.client().post(
            f"https://api.bitfinex.com/{path}", content=body, headers=headers)
        assert (response.status_code == 200) is ok, (nonce, response.text)
        if not ok:
            assert response.json() == ["error", 10114, "nonce: small"]


async def test_a_second_venue_process_loses_the_append_race_and_reloads(
        store: SqlVenueEventStore) -> None:
    clock = Clock()
    a = await make_world(funds={"UST": "1000"}, store=store, clock=clock)  # type: ignore[arg-type]
    b = await make_world(store=store, clock=clock, feed=a.feed, seed_book=False)  # type: ignore[arg-type]
    offer_id = await a.submit_ok(amount="150")  # A advances the shared log; B is now stale
    stale = await b.post("v2/auth/r/wallets", {})
    assert stale.status_code == 500  # B's nonce append lost the race: the caller is unsure
    assert [f.kind for f in b.venue.internal_failures] == ["store"]  # counted, never silent
    b.venue.internal_failures.clear()
    assert offer_id in b.venue.state.offers  # B reloaded A's committed state
    again = await b.post("v2/auth/r/wallets", {})
    assert again.status_code == 200
    assert len(await store.load(ACCOUNT)) == b.venue._seq  # B is level with the shared log again
    offers = (await b.post("v2/auth/r/funding/offers", {})).json()
    assert [row[0] for row in offers] == [offer_id]
