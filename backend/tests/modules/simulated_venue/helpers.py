"""Shared builders for simulated venue tests. T0 is far from the wall clock on purpose."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.auth_ws import sign_request
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.simulated_venue import (
    BookSnapshot,
    FaultPlan,
    FixtureMarketFeed,
    InMemoryVenueEventStore,
    PublicTrade,
    SimAccount,
    SimulatedVenue,
    SimulatedVenueConfig,
)
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue

DAY = 86_400_000
HOUR = 3_600_000
T0 = 1_704_067_200_000  # 2024-01-01T00:00Z, a UTC midnight
API_KEY = "SIM_KEY"
API_SECRET = "SIM_SECRET"
ACCOUNT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCOUNT = SimAccount(ACCOUNT_ID, "ci")
CTX = AccountContext(ACCOUNT.exchange_account_id, Credentials(API_KEY, API_SECRET), Decimal(10000))


class Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def config(**overrides: Any) -> SimulatedVenueConfig:
    return SimulatedVenueConfig(api_key=API_KEY, api_secret=API_SECRET, **overrides)


def book(symbol: str = "fUST", at: int = T0, asks: list[tuple[str, int, str]] | None = None,
         ) -> BookSnapshot:
    levels = asks if asks is not None else []
    return BookSnapshot(symbol, at, tuple((Decimal(r), p, Decimal(a)) for r, p, a in levels))


def trade(mts: int, amount: str, period: int = 2, rate: str = "0.0002") -> PublicTrade:
    return PublicTrade(mts, Decimal(amount), Decimal(rate), period)


WORLDS: list[World] = []  # every world built this test; conftest checks them at teardown


@dataclass
class World:
    venue: SimulatedVenue
    feed: FixtureMarketFeed
    clock: Clock
    store: InMemoryVenueEventStore
    gate: AuthRequestGate = field(default_factory=AuthRequestGate)
    cfg: SimulatedVenueConfig = field(default_factory=config)
    asks: list[tuple[str, int, str]] | None = None

    @property
    def rest(self) -> BitfinexAuthREST:
        """The real read client; shares this world's gate (one nonce source per key)."""
        return BitfinexAuthREST(http=self.venue.client(), auth_gate=self.gate)

    async def post(self, path: str, body: dict[str, Any], *, client: httpx.AsyncClient | None = None,
                   ) -> httpx.Response:
        """A correctly signed raw request drawing its nonce from the shared gate."""
        raw = json.dumps(body).encode()
        http = client or self.venue.client()
        async with self.gate.nonce("read", label=path) as nonce:
            headers = sign_request(body=raw, nonce=nonce, api_secret=API_SECRET, path=path)
            headers["bfx-apikey"] = API_KEY
            headers["Content-Type"] = "application/json"
            return await http.post(
                f"https://api.bitfinex.com/{path}", content=raw, headers=headers)

    async def submit(self, amount: str = "150", rate: str = "0.0002", period: int = 2,
                     symbol: str = "fUST") -> httpx.Response:
        # The venue refuses stale books; keep the fixture feed's book current, as a live
        # feed would.
        for sym in ("fUST", "fUSD"):
            self.feed.add_book(book(sym, self.clock.now, self.asks))
        return await self.post("v2/auth/w/funding/offer/submit", {
            "type": "LIMIT", "symbol": symbol, "amount": amount, "rate": rate,
            "period": period, "flags": 0,
        })

    async def submit_ok(self, **kwargs: Any) -> int:
        response = await self.submit(**kwargs)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body[6] == "SUCCESS", body
        return int(body[4][0])

    async def cancel(self, offer_id: int) -> httpx.Response:
        return await self.post("v2/auth/w/funding/offer/cancel", {"id": offer_id})


async def make_world(
    *, funds: dict[str, str] | None = None, faults: FaultPlan | None = None,
    cfg: SimulatedVenueConfig | None = None, store: InMemoryVenueEventStore | None = None,
    feed: FixtureMarketFeed | None = None, clock: Clock | None = None,
    asks: list[tuple[str, int, str]] | None = None, seed_book: bool = True,
    venue_factory: Callable[..., Any] | None = None,
) -> World:
    cfg = cfg or config()
    store = store or InMemoryVenueEventStore()
    clock = clock or Clock()
    feed = feed or FixtureMarketFeed()
    if seed_book:
        feed.add_book(book("fUST", clock.now, asks))
        feed.add_book(book("fUSD", clock.now, asks))
    venue = await (venue_factory or build_simulated_venue)(
        account=ACCOUNT, config=cfg, store=store, feed=feed, clock_ms=clock,
        faults=faults,
    )
    if funds is not None:
        for currency, amount in funds.items():
            await venue.fund_wallet(currency, Decimal(amount))
    world = World(venue, feed, clock, store, cfg=cfg, asks=asks)
    WORLDS.append(world)
    return world
