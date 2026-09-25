"""A refused live boot is an automatic stop: HALTED/auto, then the cancel-all.

SQLite and PostgreSQL. The venue, the credential loader and the writer lock are
fakes that record what they were asked; the trading state, the audit and the
vault catalog check are the real ones.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text

from bfx_funding_bot.core.errors import ExecutorAuthError
from bfx_funding_bot.modules.execution.protocols import Credentials, FundingCancelAllResult
from bfx_funding_bot.modules.execution.safety.boot_stop import stop_refused_boot
from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.observability import alerts

pytestmark = pytest.mark.integration


@pytest.fixture
def capital_db(migrated_db):
    """The migrated PostgreSQL schema: its triggers are the rules' authority."""
    return migrated_db


SYMBOLS = ("fUST", "fUSD")


class Venue:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def cancel_all_funding_offers(self, *, currency: str, ctx: Any) -> FundingCancelAllResult:
        self.calls.append((currency, ctx.credentials.api_key))
        if self.fail:
            raise ExecutorAuthError("apikey: invalid")
        return FundingCancelAllResult(outcome="acknowledged", venue_status="SUCCESS", text="ok")


class Lock:
    released = False

    async def verify_held(self) -> bool:
        return True

    async def release(self) -> None:
        self.released = True


class Harness:
    def __init__(self, factory: Any, account: Any, monkeypatch: pytest.MonkeyPatch, *,
                 venue: Venue | None = None, lock: Lock | None = None,
                 credentials_fail: bool = False) -> None:
        self.factory, self.account = factory, account
        self.venue = venue or Venue()
        self.lock = lock
        self.credentials_fail = credentials_fail
        self.credential_reads = 0
        self.alerts: list[tuple[str, str | None, dict[str, Any]]] = []
        monkeypatch.setattr(alerts, "emit", lambda event, *, level=None, **fields:
                            self.alerts.append((event, level, fields)))

    async def credentials(self, session: Any) -> Credentials:
        self.credential_reads += 1
        if self.credentials_fail:
            raise RuntimeError("vault row unreadable")
        return Credentials(api_key="key-1", api_secret="secret-1")

    async def writer_lock(self) -> Lock | None:
        return self.lock

    async def run(self):
        return await stop_refused_boot(
            session_factory=self.factory, account_id=self.account, environment="ci",
            configured_symbols=SYMBOLS, reason="boot_blocked: schema_head_mismatch database=x",
            load_credentials=self.credentials, venue=lambda: self.venue,
            writer_lock=self.writer_lock, clock=lambda: 5_000)

    async def state(self):
        return await TradingStateRepository(self.factory, account_id=self.account,
                                            deployment_environment="ci").current()

    async def audit(self) -> list[tuple[str, str, str | None]]:
        async with self.factory() as session:
            return [(row.currency, row.phase, row.detail) for row in (await session.scalars(
                select(FundingCancelAllAuditRow).order_by(FundingCancelAllAuditRow.id))).all()]

    def manual(self) -> list[dict[str, Any]]:
        return [fields for event, level, fields in self.alerts
                if event == "venue_offers_may_remain" and level == "critical"]


@pytest.mark.asyncio
async def test_a_refused_boot_halts_then_cancels_every_configured_currency(capital_db, monkeypatch):
    factory, account = capital_db
    lock = Lock()
    h = Harness(factory, account, monkeypatch, lock=lock)
    result = await h.run()
    state = await h.state()
    assert (state.state, state.cause, state.actor) == ("HALTED", "auto", "boot")
    assert result is not None and result.complete
    assert sorted(h.venue.calls) == [("USD", "key-1"), ("UST", "key-1")]
    assert sorted(await h.audit()) == [
        ("USD", "acknowledged", "ok"), ("USD", "requested", None),
        ("UST", "acknowledged", "ok"), ("UST", "requested", None)]
    assert [e for e, _, _ in h.alerts].count("kill_switch_engaged") == 1
    assert h.manual() == [] and lock.released


@pytest.mark.asyncio
async def test_a_vault_this_build_cannot_read_is_never_read_and_the_operator_is_told(
        capital_db, monkeypatch):
    """The schema changed under the credential vault: no row is read, no venue
    call is made, and a critical alert says the offers must be cancelled by hand."""
    factory, account = capital_db
    async with factory.begin() as session:
        await session.execute(text("ALTER TABLE exchange_account_credentials ADD COLUMN rotated_by TEXT"))
    h = Harness(factory, account, monkeypatch, lock=Lock())
    result = await h.run()
    assert (await h.state()).state == "HALTED"
    assert h.credential_reads == 0 and h.venue.calls == []
    assert result is not None and not result.complete
    assert sorted(await h.audit()) == [("USD", "skipped", "no_live_venue"),
                                       ("UST", "skipped", "no_live_venue")]
    [manual] = h.manual()
    assert "cancel them by hand" in manual["message"]
    assert "credential vault tables differ" in manual["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "detail"), [
    ("credentials_fail", "credentials unreadable"),
    ("no_lock", "skipped"),
    ("venue_fails", "cancel-all incomplete"),
])
async def test_whatever_stops_the_cancel_all_leaves_the_manual_alert(capital_db, monkeypatch, case, detail):
    factory, account = capital_db
    h = Harness(factory, account, monkeypatch,
                lock=None if case == "no_lock" else Lock(),
                venue=Venue(fail=case == "venue_fails"),
                credentials_fail=case == "credentials_fail")
    result = await h.run()
    assert (await h.state()).state == "HALTED"
    assert result is not None and not result.complete
    [manual] = h.manual()
    assert detail in manual["detail"]
    if case != "venue_fails":
        assert h.venue.calls == []


@pytest.mark.asyncio
async def test_no_halt_means_nothing_reaches_the_venue(capital_db, monkeypatch):
    factory, account = capital_db
    h = Harness(factory, account, monkeypatch, lock=Lock())

    async def refuse(*args, **kwargs):
        raise RuntimeError("trading_state unwritable")

    monkeypatch.setattr(TradingStateRepository, "transition", refuse)
    assert await h.run() is None
    assert h.venue.calls == [] and h.credential_reads == 0
    [manual] = h.manual()
    assert "nothing was sent to the venue" in manual["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_another_writer_holding_the_lock_owns_the_venue(pg_engine):
    from uuid import uuid4

    from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
    from bfx_funding_bot.modules.execution.safety.boot_stop import writer_lock_or_none
    url = pg_engine.url.render_as_string(hide_password=False)
    key = derive_lock_key(str(uuid4()), "ci")
    running = WriterLock(database_url=url, key=key)
    await running.acquire()
    try:
        assert await writer_lock_or_none(WriterLock(database_url=url, key=key)) is None
    finally:
        await running.release()
    mine = await writer_lock_or_none(WriterLock(database_url=url, key=key))
    assert mine is not None and await mine.verify_held()
    await mine.release()
