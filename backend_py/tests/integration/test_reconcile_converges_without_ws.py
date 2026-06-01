"""Integration: periodic reconcile converges the ledger with the WS stream DEAD.

Regression guard for a real-money incident (2026-05-26 / spec 2026-05-27):

  An offer was placed and recorded CLAIMED (ledger `reserved` rose by its size),
  then it disappeared from the venue (matched/closed). NO WS fill/release event
  was ever processed, so the reservation was never released → the ledger stayed
  pinned at the allocation cap and the bot could place nothing further.

  The fix is the runtime PeriodicReconcile backbone: with NO WS events at all,
  the periodic venue-snapshot reconcile must notice that a local CLAIMED is
  absent from the (empty) venue snapshot and publish ReservationReleased, which
  the in-memory ledger projection applies — dropping exposure back to 0.

This wires the REAL pieces end-to-end:
  - REAL DomainEventBus + REAL PaperPositionLedger + REAL OfferRegistry
    (the `domain_chain` fixture from phase4_4a — ledger is subscribed to
    ReservationReleased on this bus).
  - REAL BootRecovery.run() reconcile, publishing to that SAME real bus.
  - REAL PeriodicReconcile.run_loop driving it on an interval.
  - NO ws_dispatcher in the loop, and NO WS events fired.

If the release did not happen (e.g. if BootRecovery published to a stub bus the
ledger is not subscribed to), exposure would stay at 150 and this test FAILS —
so it is not vacuously passing.
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
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

# The `domain_chain` fixture (real DomainEventBus + real PaperPositionLedger +
# real OfferRegistry, fully subscriber-wired) is re-exported from the phase4_4a
# harness by tests/integration/conftest.py, so it is available here by name.

# BootRecovery clock — the "now" the reconcile diffs against. The seeded claim's
# occurred_at_ms must be OLDER than NOW - action_grace_ms so reconcile may act.
_NOW = 2_000_000
_ACCOUNT = "default"
_ENV = "ci"
_VOI = "777"
_CID = 777
_SIZE = Decimal("150")
_ACTION_GRACE_MS = 120_000
# Claim placed well before the action-grace window → reconcile is allowed to act.
_CLAIM_OCCURRED_MS = _NOW - 600_000  # 10 min old


class _OneRowScalars:
    """`session.execute(...)` result whose `.scalars().all()` yields one row."""

    def __init__(self, rows: list[OfferClaimRow]) -> None:
        self._rows = rows

    def scalars(self) -> _OneRowScalars:
        return self

    def all(self) -> list[OfferClaimRow]:
        return self._rows


class _OneClaimSession:
    """Async-session stub: returns a single CLAIMED OfferClaimRow on execute()."""

    def __init__(self, row: OfferClaimRow) -> None:
        self._row = row

    async def execute(self, _stmt: Any) -> _OneRowScalars:
        return _OneRowScalars([self._row])

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _OneClaimSessionCtx:
    def __init__(self, row: OfferClaimRow) -> None:
        self._row = row

    async def __aenter__(self) -> _OneClaimSession:
        return _OneClaimSession(self._row)

    async def __aexit__(self, *_args: Any) -> None:
        pass


class _OneClaimSessionFactory:
    def __init__(self, row: OfferClaimRow) -> None:
        self._row = row

    def __call__(self) -> _OneClaimSessionCtx:
        return _OneClaimSessionCtx(self._row)


class _StubStore:
    """Durable append stub — append is not what we verify (bus→ledger is)."""

    def __init__(self) -> None:
        self.appended: list[Any] = []

    async def append(self, _session: Any, event: Any) -> bool:
        self.appended.append(event)
        return True


class _EmptyAuthRest:
    """Venue snapshot is EMPTY — the offer has disappeared from the venue."""

    async def get_active_funding_offers(
        self, *, ctx: Any, symbol: str = "fUSD",
    ) -> list[Any]:
        return []


class _FakeProbe:
    """No-op probe (heartbeat + health updates), like the unit tests use."""

    def record_heartbeat(self, _sub_task: str) -> None:
        pass

    def update(self, _target: Any, _status: Any, **_fields: object) -> None:
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_periodic_reconcile_converges_ledger_with_ws_dead(
    domain_chain: dict[str, Any],
) -> None:
    bus = domain_chain["bus"]
    ledger = domain_chain["ledger"]

    scid = uuid4()

    # 1. Seed a CLAIMED reservation on the REAL bus → ledger reserved rises to 150.
    #    (occurred_at_ms here is just event metadata; the ledger only adds size.)
    await bus.publish(ReservationClaimed(
        cid=_CID, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=scid, account_id=_ACCOUNT, is_simulated=False,
        occurred_at_ms=_CLAIM_OCCURRED_MS,
    symbol="fUST"))

    # Precondition: exposure MUST be 150 before reconcile, else the test proves nothing.
    assert ledger.current_exposure() == Decimal("150")

    # The same CLAIMED reservation as it lives in the snapshot table. _load_local_claims
    # reads this and yields one CLAIMED LocalClaim; its occurred_at_ms is older than the
    # action-grace window so the missing-from-venue release is allowed to fire.
    # The snapshot stores RegistryState.value (lowercase "claimed"); _load_local_claims
    # round-trips it via RegistryState(r.state), so we use the same stored form here.
    claim_row = OfferClaimRow(
        cid=_CID, account_id=_ACCOUNT, deployment_environment=_ENV,
        state=RegistryState.CLAIMED.value, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=str(scid), occurred_at_ms=_CLAIM_OCCURRED_MS,
        last_updated_ms=_CLAIM_OCCURRED_MS, last_event_seq=1,
    )

    # 2. REAL BootRecovery: empty venue snapshot + the one local CLAIMED row,
    #    publishing to the SAME real bus the ledger is subscribed to.
    recovery = BootRecovery(
        store=_StubStore(),  # type: ignore[arg-type]
        session_factory=_OneClaimSessionFactory(claim_row),  # type: ignore[arg-type]
        auth_rest=_EmptyAuthRest(),
        account_ctx=AccountContext(
            account_id=_ACCOUNT,
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("450"),
        ),
        deployment_environment=_ENV,
        bus=bus,  # REAL bus from domain_chain — ledger reacts to ReservationReleased here
        is_simulated=False,
        action_grace_ms=_ACTION_GRACE_MS,
        max_attempts=1,
        backoff_base_s=0,
        clock=lambda: _NOW,
    )

    # 3. REAL PeriodicReconcile loop — NO ws_dispatcher, NO WS events fired.
    pr = PeriodicReconcile(
        recovery=recovery, probe=_FakeProbe(), interval_s=0.01,
        max_consecutive_failures=3,
    )
    stop = asyncio.Event()

    async def _stop_after_a_tick() -> None:
        # Let a couple of ticks run, then stop.
        await asyncio.sleep(0.035)
        stop.set()

    await asyncio.gather(pr.run_loop(stop), _stop_after_a_tick())

    # 4. The stuck reservation was released purely by the periodic reconcile →
    #    exposure converged to 0 with the WS stream completely dead.
    assert ledger.current_exposure() == Decimal("0")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_run_reports_the_release_as_divergence(
    domain_chain: dict[str, Any],
) -> None:
    """A single reconcile run on the same setup reports exactly one release.

    Guards that the convergence above is driven by a real missing-from-venue
    release (ReconcileResult.n_released == 1), not some incidental zeroing.
    """
    bus = domain_chain["bus"]
    scid = uuid4()

    await bus.publish(ReservationClaimed(
        cid=_CID, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=scid, account_id=_ACCOUNT, is_simulated=False,
        occurred_at_ms=_CLAIM_OCCURRED_MS,
    symbol="fUST"))

    claim_row = OfferClaimRow(
        cid=_CID, account_id=_ACCOUNT, deployment_environment=_ENV,
        state=RegistryState.CLAIMED.value, venue_offer_id=_VOI, size_usdt=_SIZE,
        signal_correlation_id=str(scid), occurred_at_ms=_CLAIM_OCCURRED_MS,
        last_updated_ms=_CLAIM_OCCURRED_MS, last_event_seq=1,
    )

    recovery = BootRecovery(
        store=_StubStore(),  # type: ignore[arg-type]
        session_factory=_OneClaimSessionFactory(claim_row),  # type: ignore[arg-type]
        auth_rest=_EmptyAuthRest(),
        account_ctx=AccountContext(
            account_id=_ACCOUNT,
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("450"),
        ),
        deployment_environment=_ENV,
        bus=bus,
        is_simulated=False,
        action_grace_ms=_ACTION_GRACE_MS,
        max_attempts=1,
        backoff_base_s=0,
        clock=lambda: _NOW,
    )

    result = await recovery.run()

    assert isinstance(result, ReconcileResult)
    assert result.n_released == 1
    assert result.n_claimed == 0
    assert result.n_failed == 0
