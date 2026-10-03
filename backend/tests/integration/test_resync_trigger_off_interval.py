"""Integration: a resync trigger (as auth_ws fires on reconnect / seq-gap) makes
PeriodicReconcile run a reconcile OFF the interval — far sooner than the timer.
Real DomainEventBus + ledger + OfferRegistry + BootRecovery + PeriodicReconcile.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.execution.boot_recovery import BootRecovery, ReconcileResult
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed
from bfx_funding_bot.modules.execution.observation_sink import LegacyObservationSink
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.ledger import Scope

# Reuse the stub session/store/auth-rest shapes from the WS-dead integration test.
from tests.integration.test_reconcile_converges_without_ws import (
    _EmptyAuthRest,
    _FakeProbe,
    _OneClaimSessionFactory,
    _StubStore,
)

from .conftest import make_reservation_ref

_NOW = 2_000_000
_ACCOUNT = "default"
_ENV = "ci"
_VOI = "777"
_CID = 777
_SIZE = Decimal("150")
_ACTION_GRACE_MS = 120_000
_CLAIM_OCCURRED_MS = _NOW - 600_000


class _CountingRecovery:
    """Wraps a real BootRecovery, recording each run() so we can assert the
    trigger produced an off-interval reconcile (independent of dedup)."""

    def __init__(self, inner: BootRecovery) -> None:
        self._inner = inner
        self.calls = 0

    async def run(self) -> ReconcileResult:
        self.calls += 1
        return await self._inner.run()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resync_request_reconciles_off_interval(
    domain_chain: dict[str, Any],
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]
    scid = uuid4()

    await bus.publish(ReservationClaimed(
        cid=_CID, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=scid, account_id=_ACCOUNT, is_simulated=False,
        occurred_at_ms=_CLAIM_OCCURRED_MS,
        symbol="fUST",
        reservation_ref=make_reservation_ref(_CID, scid, _VOI),
    ))
    assert ledger.current_exposure("fUST") == Decimal("150")

    claim_row = OfferClaimRow(
        cid=_CID, account_id=_ACCOUNT, deployment_environment=_ENV,
        state=RegistryState.CLAIMED.value, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=str(scid), occurred_at_ms=_CLAIM_OCCURRED_MS,
        last_updated_ms=_CLAIM_OCCURRED_MS, last_event_seq=1, symbol="fUST",
        execution_decision_id=f"reconcile-test-{_CID}",
    )
    inner = BootRecovery(
        store=_StubStore(),  # type: ignore[arg-type]
        session_factory=_OneClaimSessionFactory(claim_row),  # type: ignore[arg-type]
        auth_rest=_EmptyAuthRest(),
        account_ctx=AccountContext(
            account_id=_ACCOUNT,
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("450"),
        ),
        deployment_environment=_ENV, bus=bus, is_simulated=False,
        action_grace_ms=_ACTION_GRACE_MS, max_attempts=1, backoff_base_s=0,
        clock=lambda: _NOW, symbol="fUST",
    )
    recovery = _CountingRecovery(inner)

    # Huge interval: only an off-interval resync can produce a second reconcile.
    scope = Scope(uuid4(), _ENV)
    pr = PeriodicReconcile(resync=ResyncChannel(),
        recovery=LegacyObservationSink(recovery, scope), scope=scope, probe=_FakeProbe(), interval_s=3600.0,
        max_consecutive_failures=3, min_resync_interval_s=0.0,
    )
    stop = asyncio.Event()

    async def _drive() -> None:
        await asyncio.sleep(0.05)
        assert recovery.calls == 1                       # tick 1 (loop start) only
        assert ledger.current_exposure("fUST") == Decimal("0")  # ...and it really converged
        pr.resync.request("reconnect")                   # the trigger under test
        await asyncio.sleep(0.1)
        assert recovery.calls >= 2                        # off-interval reconcile ran
        stop.set()

    await asyncio.wait_for(asyncio.gather(pr.run_loop(stop), _drive()), timeout=5.0)
