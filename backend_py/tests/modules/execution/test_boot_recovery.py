from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.execution.boot_recovery import (
    BootRecovery,
    LocalClaim,
    ReconcileResult,
    compute_recovery_actions,
    synth_orphan_cid,
    synth_orphan_scid,
)
from bfx_funding_bot.modules.execution.events import (
    ReservationClaimed,
    ReservationFailed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

_ACC = "default"
_NOW = 2_000_000


def _offer(voi="555", amount="100", rate=0.0003, period=2):
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol="fUSD", amount=Decimal(amount),
        rate=rate, period_days=period, mts_created=1_000_000, status="ACTIVE",
    )


def _claim(cid, voi, state, size="100", scid=None, occurred=0):
    return LocalClaim(
        cid=cid, venue_offer_id=voi, state=state, size_usdt=Decimal(size),
        signal_correlation_id=scid or uuid4(), occurred_at_ms=occurred,
    )


def _actions(venue, local, grace_ms=120_000, now=_NOW):
    return compute_recovery_actions(
        venue_offers=venue, local_claims=local, account_id=_ACC,
        is_simulated=False, now_ms=now, grace_ms=grace_ms,
    )


def test_orphan_at_venue_is_claimed():
    acts = _actions([_offer(voi="555", amount="250")], [])
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationClaimed)
    assert ev.venue_offer_id == "555"
    assert ev.size_usdt == Decimal("250")
    assert ev.cid == synth_orphan_cid("555")
    assert ev.signal_correlation_id == synth_orphan_scid("555")
    assert ev.account_id == _ACC and ev.is_simulated is False


def test_local_claimed_missing_from_venue_is_released():
    scid = uuid4()
    claim = _claim(cid=42, voi="999", state=RegistryState.CLAIMED, size="80", scid=scid)
    acts = _actions([], [claim])
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationReleased)
    assert ev.cid == 42 and ev.venue_offer_id == "999"
    assert ev.size_usdt == Decimal("80") and ev.reason == "missing_from_venue"
    assert ev.signal_correlation_id == scid


def test_claimed_still_present_is_noop():
    claim = _claim(cid=42, voi="555", state=RegistryState.CLAIMED)
    assert _actions([_offer(voi="555")], [claim]) == []


def test_stale_pending_converges_to_failed():
    scid = uuid4()
    claim = _claim(cid=7, voi=None, state=RegistryState.PENDING, size="60",
                   scid=scid, occurred=_NOW - 200_000)  # older than grace
    acts = _actions([], [claim], grace_ms=120_000)
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationFailed)
    assert ev.cid == 7 and ev.size_usdt == Decimal("60")
    assert ev.reason == "unresolved_at_boot" and ev.signal_correlation_id == scid


def test_recent_pending_within_grace_is_left_alone():
    claim = _claim(cid=7, voi=None, state=RegistryState.PENDING,
                   occurred=_NOW - 1_000)  # within grace
    assert _actions([], [claim], grace_ms=120_000) == []


def test_synth_cid_numeric_voi_is_negated():
    assert synth_orphan_cid("12345") == -12345


def test_synth_cid_nonnumeric_fallback_is_deterministic_and_negative():
    from bfx_funding_bot.external.bitfinex.cid import BITFINEX_CID_MAX
    a = synth_orphan_cid("abc-xyz")
    assert a == synth_orphan_cid("abc-xyz")          # deterministic
    assert -BITFINEX_CID_MAX <= a < 0                # negative namespace, in range


def test_synth_scid_is_deterministic():
    assert synth_orphan_scid("555") == synth_orphan_scid("555")
    assert synth_orphan_scid("555") != synth_orphan_scid("556")
    assert isinstance(synth_orphan_scid("555"), UUID)


class _FailingAuthRest:
    def __init__(self, *, status_code, succeed_after=None):
        self.calls = 0
        self._status_code = status_code
        self._succeed_after = succeed_after  # if set, succeed (return []) on this call number
    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        self.calls += 1
        if self._succeed_after is not None and self.calls >= self._succeed_after:
            return []
        raise BitfinexAPIError(status_code=self._status_code, message="boom")


def _boot_recovery(auth_rest, **kw):
    return BootRecovery(
        store=None, session_factory=None, auth_rest=auth_rest,  # type: ignore[arg-type]
        account_ctx=AccountContext(
            account_id="default",
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("1"),
        ),
        deployment_environment="ci", bus=None,  # type: ignore[arg-type]
        max_attempts=3, backoff_base_s=0, **kw,
    )


def test_action_grace_skips_recent_orphan():
    # offer created 50s before now; action_grace_ms=120s → too fresh to claim
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=_NOW - 50_000, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert acts == []


def test_action_grace_skips_recent_missing_claim():
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 50_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert acts == []


def test_action_grace_releases_stale_missing_claim():
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 300_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert len(acts) == 1
    assert isinstance(acts[0], ReservationReleased)
    assert acts[0].venue_offer_id == "555"


def test_action_grace_claims_stale_orphan():
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=_NOW - 300_000, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
    )
    assert len(acts) == 1 and isinstance(acts[0], ReservationClaimed)


def test_action_grace_zero_preserves_boot_behaviour():
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=_NOW - 1, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000,
    )
    assert len(acts) == 1 and isinstance(acts[0], ReservationClaimed)


class _StubStore:
    """Minimal PostgresEventStore stub — records appended events + snapshot calls, no DB."""
    def __init__(self):
        self.appended: list = []
        self.snapshot_calls: list[dict] = []

    async def append(self, session, event):
        self.appended.append(event)
        return True

    async def set_position_snapshot(
        self, session, *, account_id, reserved_usdt, realized_usdt,
        n_offers, n_credits, occurred_at_ms,
    ):
        from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift
        self.snapshot_calls.append({
            "account_id": account_id,
            "reserved_usdt": reserved_usdt,
            "realized_usdt": realized_usdt,
            "n_offers": n_offers,
            "n_credits": n_credits,
            "occurred_at_ms": occurred_at_ms,
        })
        return SnapshotDrift(reserved_drift=Decimal("0"), realized_drift=Decimal("0"))


class _StubSession:
    """Async context manager stub for AsyncSession."""
    async def execute(self, stmt):
        return _EmptyScalars()
    async def commit(self):
        pass
    async def rollback(self):
        pass


class _EmptyScalars:
    def scalars(self):
        return self
    def all(self):
        return []


class _StubSessionFactory:
    """Minimal async_sessionmaker stub — yields a _StubSession."""
    def __call__(self):
        return _StubSessionCtx()


class _StubSessionCtx:
    async def __aenter__(self):
        return _StubSession()
    async def __aexit__(self, *args):
        pass


class _StubBus:
    def __init__(self):
        self.published: list = []
    async def publish(self, event):
        self.published.append(event)


class _StubAuthRest:
    """Offers+credits stub; credits defaults to [] for tests focused on offer reconciliation."""
    def __init__(self, offers, credits=None):
        self._offers = offers
        self._credits = credits if credits is not None else []

    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        return self._credits


def _full_boot_recovery(auth_rest, store, session_factory, bus, **kw):
    """Construct a BootRecovery with all real stubs wired (for run() tests)."""
    return BootRecovery(
        store=store,
        session_factory=session_factory,
        auth_rest=auth_rest,
        account_ctx=AccountContext(
            account_id="default",
            credentials=Credentials(api_key="k", api_secret="s"),
            allocation_cap_usdt=Decimal("1"),
        ),
        deployment_environment="ci",
        bus=bus,
        max_attempts=1,
        backoff_base_s=0,
        clock=lambda: _NOW,
        **kw,
    )


@pytest.mark.asyncio
async def test_run_returns_reconcile_result_for_orphan_claim():
    """run() returns ReconcileResult; venue has one orphan offer -> n_claimed=1."""
    # _StubSession returns no rows, so local_claims will be []
    # → no release; but we want to test a release scenario.
    # Use action_grace_ms=0 (boot default) with a stale CLAIMED offer absent from venue.
    # We can't inject rows via _StubSession.execute easily, so test the orphan-claim path:
    # venue has one offer, local has none → n_claimed=1.
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRest([_offer(voi="999", amount="200")])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()

    assert isinstance(result, ReconcileResult)
    assert result.n_claimed == 1
    assert result.n_released == 0
    assert result.n_failed == 0
    # PositionReconciled is published first, then domain events
    assert any(isinstance(e, ReservationClaimed) for e in bus.published)
    assert any(isinstance(e, PositionReconciled) for e in bus.published)


@pytest.mark.asyncio
async def test_run_accepts_and_threads_action_grace_ms():
    """action_grace_ms=120_000 with a freshly-created offer → n_claimed=0 (grace skips it)."""
    fresh_offer = ActiveFundingOffer(
        venue_offer_id="888", symbol="fUSD", amount=Decimal("100"),
        rate=0.0003, period_days=2,
        mts_created=_NOW - 50_000,  # 50s ago — within 120s grace
        status="ACTIVE",
    )
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRest([fresh_offer])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus, action_grace_ms=120_000)

    result = await rec.run()

    assert isinstance(result, ReconcileResult)
    assert result.n_claimed == 0   # grace skipped the fresh orphan
    assert result.n_released == 0
    assert result.n_failed == 0


@pytest.mark.asyncio
async def test_fetch_offers_does_not_retry_4xx():
    auth = _FailingAuthRest(status_code=401)
    rec = _boot_recovery(auth)
    with pytest.raises(BitfinexAPIError):
        await rec._fetch_offers()
    assert auth.calls == 1  # no retry on auth error


@pytest.mark.asyncio
async def test_fetch_offers_retries_5xx_then_succeeds():
    auth = _FailingAuthRest(status_code=503, succeed_after=3)
    rec = _boot_recovery(auth)
    result = await rec._fetch_offers()
    assert result == []
    assert auth.calls == 3  # retried twice, succeeded on 3rd


@pytest.mark.asyncio
async def test_fetch_offers_reraises_after_transient_exhaustion():
    auth = _FailingAuthRest(status_code=0)  # transport error, never succeeds
    rec = _boot_recovery(auth)
    with pytest.raises(BitfinexAPIError):
        await rec._fetch_offers()
    assert auth.calls == 3  # exhausted max_attempts


# ── Credit-aware reconcile (Phase 4.4d / 2026-05-29) ─────────────────────────


from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingCredit  # noqa: E402
from bfx_funding_bot.modules.execution.events import PositionReconciled  # noqa: E402


def _credit(credit_id: str = "1", amount: str = "150") -> ActiveFundingCredit:
    return ActiveFundingCredit(
        credit_id=credit_id, symbol="fUST",
        amount=Decimal(amount), rate=0.0003, period_days=2, status="ACTIVE",
    )


class _StubAuthRestFull:
    """Satisfies both _ActiveOffersQuery and _ActiveCreditsQuery protocols."""
    def __init__(self, offers, credits):
        self._offers = offers
        self._credits = credits

    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        return self._credits


@pytest.mark.asyncio
async def test_run_emits_position_reconciled_with_credit_sum():
    """run() fetches credits and publishes PositionReconciled(realized=Σcredits)."""
    credits = [_credit("1", "150"), _credit("2", "150"), _credit("3", "150")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=credits)
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    await rec.run()

    pr_events = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr_events) == 1
    pr = pr_events[0]
    assert pr.realized_usdt == Decimal("450")
    assert pr.reserved_usdt == Decimal("0")
    assert pr.n_credits == 3
    assert pr.n_offers == 0


@pytest.mark.asyncio
async def test_run_calls_set_position_snapshot_with_credit_sum():
    """run() calls store.set_position_snapshot with absolute venue values."""
    credits = [_credit("1", "300")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=credits)
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    await rec.run()

    assert len(store.snapshot_calls) == 1
    call = store.snapshot_calls[0]
    assert call["realized_usdt"] == Decimal("300")
    assert call["reserved_usdt"] == Decimal("0")
    assert call["n_credits"] == 1


@pytest.mark.asyncio
async def test_run_position_reconciled_includes_offer_reserved():
    """reserved = Σ(active offers) from venue, not from event accumulation."""
    offers = [_offer(voi="555", amount="100")]
    credits = [_credit("1", "200")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=offers, credits=credits)
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    await rec.run()

    pr_events = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr_events) == 1
    pr = pr_events[0]
    assert pr.reserved_usdt == Decimal("100")
    assert pr.realized_usdt == Decimal("200")
    assert pr.n_offers == 1
    assert pr.n_credits == 1


@pytest.mark.asyncio
async def test_run_credits_fetch_failure_raises():
    """Credits fetch failure at boot → fail-fast (same as offers-fetch failure)."""
    class _FailCredits:
        async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
            return []

        async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
            raise BitfinexAPIError(status_code=401, message="auth error")

    store = _StubStore()
    bus = _StubBus()
    rec = _full_boot_recovery(_FailCredits(), store, _StubSessionFactory(), bus)

    with pytest.raises(BitfinexAPIError):
        await rec.run()


@pytest.mark.asyncio
async def test_run_reconcile_result_includes_credit_dimensions():
    """ReconcileResult carries realized_usdt, reserved_usdt, n_credits."""
    credits = [_credit("1", "200")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=credits)
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()

    assert isinstance(result, ReconcileResult)
    assert result.realized_usdt == Decimal("200")
    assert result.reserved_usdt == Decimal("0")
    assert result.n_credits == 1


@pytest.mark.asyncio
async def test_run_threads_drift_from_snapshot_into_result():
    """ReconcileResult carries realized_drift/reserved_drift from set_position_snapshot."""
    from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift

    class _DriftStore(_StubStore):
        async def set_position_snapshot(self, session, **kw):
            await super().set_position_snapshot(session, **kw)
            return SnapshotDrift(reserved_drift=Decimal("0"), realized_drift=Decimal("150"))

    store = _DriftStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=[_credit("1", "450")])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()

    assert result.realized_drift_usdt == Decimal("150")
    assert result.reserved_drift_usdt == Decimal("0")
