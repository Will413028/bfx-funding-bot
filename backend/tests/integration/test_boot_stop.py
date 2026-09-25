"""A refused live boot is an automatic stop: HALTED/auto and an alert, no venue write.

Lending envelope D2/D3: an automatic reaction never cancels offers it cannot
prove are its own, and a refused boot has no command gate to prove it with.
SQLite and PostgreSQL; the trading state is the real one.
"""
from __future__ import annotations

from typing import Any

import pytest

from bfx_funding_bot.modules.execution.safety.boot_stop import (
    VENUE_OFFERS_MAY_REMAIN,
    stop_refused_boot,
)
from bfx_funding_bot.modules.execution.safety.trading_state import TradingStateRepository
from bfx_funding_bot.modules.observability import alerts

pytestmark = pytest.mark.integration


@pytest.fixture
def capital_db(migrated_db):
    """The migrated PostgreSQL schema: its triggers are the rules' authority."""
    return migrated_db


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    captured: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(alerts, "emit", lambda event, **fields: captured.append((event, fields)))
    return captured


@pytest.mark.asyncio
async def test_a_refused_boot_halts_and_tells_the_operator(capital_db, sent):
    factory, account = capital_db
    trading = TradingStateRepository(factory, account_id=account, deployment_environment="ci")
    await trading.transition("ACTIVE", cause="operator", actor="will", reason="trading", now_ms=1)
    state = await stop_refused_boot(session_factory=factory, account_id=account, environment="ci",
                                    reason="boot_blocked: schema head mismatch", clock=lambda: 5)
    assert state is not None and (state.state, state.cause, state.actor) == ("HALTED", "auto", "boot")
    assert (await trading.current()) == state
    offers_alerts = [fields for event, fields in sent if event == VENUE_OFFERS_MAY_REMAIN]
    assert len(offers_alerts) == 1 and offers_alerts[0]["level"] == "critical"
    assert "kill switch" in offers_alerts[0]["message"]


@pytest.mark.asyncio
async def test_a_halt_that_cannot_be_written_is_still_reported(capital_db, sent):
    _, account = capital_db

    class Broken:
        def begin(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("database gone")

    state = await stop_refused_boot(session_factory=Broken(), account_id=account,  # type: ignore[arg-type]
                                    environment="ci", reason="boot_blocked: x", clock=lambda: 5)
    assert state is None
    assert [fields["detail"] for event, fields in sent if event == VENUE_OFFERS_MAY_REMAIN] == [
        "HALTED could not be written (RuntimeError)"]
