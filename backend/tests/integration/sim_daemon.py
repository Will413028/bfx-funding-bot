"""A simulated-venue bot process built by ``bot.build_daemon`` on migrated PostgreSQL.

The composition is production's: phase ``shadow`` (venue ``simulated``), the real command
gate, safety chain, reconciler, ledger authority and ``BitfinexLiveExecutor`` talking to the
simulated venue through its own ``httpx`` client. What a test controls:

* the composition clock (``bot.now_ms_utc`` is patched, and the venue follows it);
* the venue's market feed and fault plan, through ``VenueSeam`` (production passes neither);
* the bot's own funding book, written into its store with the same clock.

The ledger epoch is a REAL row appended as the owner (no ``read_authority`` patch), the
database is stamped ``ci`` by the shared seed, and public endpoints are answered by
``httpx_mock``. Authenticated traffic never reaches ``httpx_mock``: ``auth_requests`` lists
any request to the authenticated host that did.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bfx_funding_bot.apps import bot
from bfx_funding_bot.apps.venue import VenueSeam
from bfx_funding_bot.external.bitfinex.rest import FundingBookLevel
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuote
from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger.tables import ExecutionResolutionJournalRow
from bfx_funding_bot.modules.ledger.wiring import build_policy_store
from bfx_funding_bot.modules.simulated_venue import (
    BookSnapshot,
    FaultPlan,
    FixtureMarketFeed,
    PublicTrade,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome
from bfx_funding_bot.modules.trading import CapitalPolicy, OfferEnvelope
from tests.modules.marketfeed.account_test_helpers import (
    TEST_EXCHANGE_ACCOUNT_ID,
    seed_exchange_account,
)
from tests.modules.marketfeed.test_daemon_wiring import _write_cells_yaml

SCOPE = Scope(TEST_EXCHANGE_ACCOUNT_ID, "ci")
CELL = "fUST_a30"
T0 = 1_704_067_200_000  # 2024-01-01T00:00Z, a UTC midnight, far from the wall clock
HOUR = 3_600_000
DAY = 86_400_000
CONSERVED = {"baseline", "conserved"}
POLICY = CapitalPolicy(
    enabled=True, max_cell_fraction=Decimal(1), max_offer_amount=Decimal("500"),
    envelope=OfferEnvelope(
        min_period_days=2, max_period_days=2, max_open_offers=6,
        rate_floor_ratio=Decimal("0.5"), min_rate_apr=Decimal("0.01")),
)
AUTH_HOST = "api.bitfinex.com"
ENV = {
    "BFX_PHASE": "shadow", "BFX_DEPLOYMENT_ENV": "ci",
    "BFX_EXECUTION_POLICY": "book_guarded", "BFX_BOOK_MAX_AGE_SECONDS": "30",
    "BFX_BOOK_RECONCILE_INTERVAL_SECONDS": "15", "BFX_BOOK_MAX_DOWN_PCT": "0.15",
    "BFX_SERVICE_VERSION": "test", "BFX_HEALTHZ_PORT": "0",
    "BFX_SIM_INITIAL_WALLETS": "UST:1000",
}
LEGACY_ENV = (
    "BFX_ALLOCATION_CAP_USDT", "BFX_BALANCE_BUFFER_USDT", "BFX_CONCENTRATION_PCT",
    "BFX_VENUE_FLOOR_USD", "BFX_MIN_OFFER_BUFFER_PCT", "BFX_EXECUTOR", "BFX_VAULT_KEK",
    "BFX_WS_CLIENT_ENABLED", "BFX_FILL_TRACKER_ENABLED", "BFX_ACCOUNT_ID",
)


class FakeClock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


@dataclass
class SimEnv:
    url: str
    factory: async_sessionmaker[AsyncSession]
    clock: FakeClock
    feed: FixtureMarketFeed
    cells_path: Path
    httpx_mock: Any
    daemons: list[Any] = field(default_factory=list)

    async def build(self, *, faults: FaultPlan | None = None, feed: Any = None) -> Any:
        daemon = await bot.build_daemon(
            cells_yaml_path=self.cells_path, skip_ws=True,
            venue_seam=VenueSeam(feed=feed or self.feed, faults=faults))
        self.daemons.append(daemon)
        self.give_the_bot_a_book(daemon)
        return daemon

    async def restart(self, daemon: Any) -> Any:
        """The old process exits (writer lock released); a new one is composed on the same DB."""
        await daemon.writer_lock.release()
        daemon.writer_lock = None
        return await self.build()

    # -- market data -----------------------------------------------------------------
    def refresh_venue_books(self, asks: list[tuple[str, int, str]] | None = None) -> None:
        levels = asks if asks is not None else [("0.0003", 2, "800"), ("0.0004", 30, "400")]
        for symbol in ("fUST", "fUSD"):
            self.feed.add_book(BookSnapshot(symbol, self.clock.now, tuple(
                (Decimal(r), p, Decimal(a)) for r, p, a in levels)))

    def give_the_bot_a_book(self, daemon: Any) -> None:
        """The bot's own funding book (its store runs on the composition clock)."""
        store = daemon.funding_book_service._store
        store._clock = self.clock
        levels = [FundingBookLevel(0.0003, 2, 3, 5000.0), FundingBookLevel(0.0002, 2, 2, -5000.0)]
        store.apply_snapshot("fUST", levels, sequence=None)

    def quote(self, daemon: Any, *, rate: str = "0.0003", period: int = 2) -> None:
        daemon.periodic_reconcile._deployment._store.update(StandingQuote(
            cell_id=CELL, outcome=DecisionOutcome.POST, rate=Decimal(rate), period_days=period,
            signal_correlation_id=uuid4(), created_at_ms=self.clock.now))

    def trades(self, *trades: tuple[int, str, int, str]) -> None:
        self.feed.add_trades("fUST", [
            PublicTrade(mts, Decimal(amount), Decimal(rate), period)
            for mts, amount, period, rate in trades])

    # -- driving ---------------------------------------------------------------------
    async def boot(self, daemon: Any, at: int | None = None) -> None:
        if at is not None:
            self.clock.now = at
        await daemon._run_boot_recovery()

    async def tick(self, daemon: Any) -> None:
        """One periodic reconcile: observe the venue, then deploy (the real reconciler)."""
        self.refresh_venue_books()
        self.give_the_bot_a_book(daemon)
        await daemon.periodic_reconcile._tick()

    async def activate(self) -> None:
        """The operator's ACTIVE: a fresh account is HALTED until someone says otherwise."""
        from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository

        await TradingStateRepository(
            self.factory, account_id=TEST_EXCHANGE_ACCOUNT_ID, deployment_environment="ci",
        ).transition("ACTIVE", cause="operator", actor="test", reason="simulated run",
                     now_ms=self.clock.now)

    # -- invariants ------------------------------------------------------------------
    def venue(self, daemon: Any) -> Any:
        return daemon.venue_diagnostics

    async def assert_sound(self, daemon: Any, *, allow_unknown: bool = False) -> None:
        """After a cycle: every accepted basis conserved, nothing unexplained or quarantined."""
        async with self.factory() as session:
            rows = (await session.execute(text(
                "SELECT symbol, conservation, lent_unexplained, foreign_executed, fill_conflicts "
                "FROM accepted_capital_basis_symbol"))).all()
            quarantines = await session.scalar(text("SELECT count(*) FROM quarantine_opening"))
        assert rows, "no accepted basis yet"
        for symbol, verdict, unexplained, foreign, conflicts in rows:
            assert verdict in CONSERVED, (symbol, verdict, unexplained, foreign, conflicts)
            assert (unexplained, foreign, conflicts) == (0, 0, 0), (symbol, verdict)
        assert quarantines == 0
        self.assert_venue_clean(daemon)

    def assert_venue_clean(self, daemon: Any) -> None:
        venue = self.venue(daemon)
        assert venue.internal_failures == [] and venue.unexpected == []
        assert self.auth_requests() == []

    def auth_requests(self) -> list[str]:
        """Authenticated-host requests that escaped the simulated venue into the real client."""
        return [str(r.url) for r in self.httpx_mock.get_requests() if r.url.host == AUTH_HOST]

    async def resolutions(self) -> int:
        async with self.factory() as session:
            return int(await session.scalar(
                select(func.count()).select_from(ExecutionResolutionJournalRow)) or 0)


async def make_sim_env(ledger_db: Any, monkeypatch: Any, httpx_mock: Any, tmp_path: Path, *,
                       epoch: str | None = "ledger", policy: bool = True,
                       env: dict[str, str] | None = None) -> tuple[SimEnv, Any]:
    url = ledger_db.url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(TEST_EXCHANGE_ACCOUNT_ID))
    for name in list(os.environ):
        if name.startswith("BFX_CANARY_") or name in LEGACY_ENV:
            monkeypatch.delenv(name)
    for name, value in {**ENV, "DATABASE_URL": url,
                        "BFX_SAFETY_CONFIG": str(Path(__file__).parents[2] / "configs/safety.live.yaml"),
                        **(env or {})}.items():
        monkeypatch.setenv(name, value)
    await seed_exchange_account(engine, capital_policies=False)
    if epoch is not None:
        async with engine.begin() as conn:  # the owner's switch: a real epoch row
            await conn.execute(text(
                "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
                "VALUES (2, :authority, 2, 'test', 'simulation')"), {"authority": epoch})
    if policy:
        store = build_policy_store(SCOPE)
        async with factory.begin() as session:
            await store.apply_policy(session, symbol="fUST", policy=POLICY, expected_revision=0,
                                     source={"fixture": True})
            await store.apply_policy(session, symbol="fUSD", policy=CapitalPolicy(enabled=False),
                                     expected_revision=0, source={"fixture": True})
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/.*"),
                            method="GET", json=[], is_reusable=True, is_optional=True)
    httpx_mock.add_response(url=re.compile(r"https://api-pub\.bitfinex\.com/v2/calc/fx"),
                            method="POST", json=[1], is_reusable=True, is_optional=True)
    from bfx_funding_bot.modules.observability import alerts
    sent: list[str] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: sent.append(event))
    clock = FakeClock()
    monkeypatch.setattr(bot, "now_ms_utc", clock)
    sim = SimEnv(url, factory, clock, FixtureMarketFeed(), _write_cells_yaml(tmp_path), httpx_mock)
    sim.feed.add_book(BookSnapshot("fUST", clock.now, ((Decimal("0.0003"), 2, Decimal("800")),)))
    return sim, engine


async def close_sim_env(sim: SimEnv, engine: Any) -> None:
    for daemon in sim.daemons:
        if daemon.writer_lock is not None:
            await daemon.writer_lock.release()
        await daemon.bitfinex_http.aclose()
        if daemon.venue_client is not None:
            await daemon.venue_client.aclose()
        await daemon.db_engine.dispose()
    await engine.dispose()
