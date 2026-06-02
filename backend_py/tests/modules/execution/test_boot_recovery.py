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


def _claim(cid, voi, state, size="100", scid=None, occurred=0, symbol="fUST"):
    return LocalClaim(
        cid=cid, venue_offer_id=voi, state=state, size_usdt=Decimal(size),
        signal_correlation_id=scid or uuid4(), occurred_at_ms=occurred,
        symbol=symbol,
    )


def _actions(venue, local, grace_ms=120_000, now=_NOW):
    return compute_recovery_actions(
        venue_offers=venue, local_claims=local, account_id=_ACC,
        is_simulated=False, now_ms=now, grace_ms=grace_ms,
        configured_symbols=frozenset({"fUSD", "fUST"}),
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
    # Preserve the old default-fUSD behaviour for callers that pass neither
    # symbol nor symbols (BootRecovery now fails loud if both are absent).
    if "symbol" not in kw and "symbols" not in kw:
        kw["symbol"] = "fUSD"
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
        configured_symbols=frozenset({"fUSD", "fUST"}),
    )
    assert acts == []


def test_action_grace_skips_recent_missing_claim():
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 50_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
        configured_symbols=frozenset({"fUSD", "fUST"}),
    )
    assert acts == []


def test_action_grace_releases_stale_missing_claim():
    claim = _claim(cid=1, voi="555", state=RegistryState.CLAIMED, occurred=_NOW - 300_000)
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000, action_grace_ms=120_000,
        configured_symbols=frozenset({"fUSD", "fUST"}),
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
        configured_symbols=frozenset({"fUSD", "fUST"}),
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
        configured_symbols=frozenset({"fUSD", "fUST"}),
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
        self, session, *, account_id, symbol, reserved_usdt, realized_usdt,
        n_offers, n_credits, occurred_at_ms,
    ):
        from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift
        self.snapshot_calls.append({
            "account_id": account_id,
            "symbol": symbol,
            "reserved": reserved_usdt,
            "realized": realized_usdt,
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
    """Offers+credits+wallet-available stub for run() tests."""
    def __init__(self, offers, credits=None, available=Decimal("0")):
        self._offers = offers
        self._credits = credits if credits is not None else []
        self._available = available

    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        return self._credits

    async def get_funding_available(self, *, ctx, currency):
        return self._available


def _full_boot_recovery(auth_rest, store, session_factory, bus, **kw):
    """Construct a BootRecovery with all real stubs wired (for run() tests)."""
    # Preserve the old default-fUSD behaviour for callers that pass neither
    # symbol nor symbols (BootRecovery now fails loud if both are absent).
    if "symbol" not in kw and "symbols" not in kw:
        kw["symbol"] = "fUSD"
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
        await rec._fetch_offers("fUSD")
    assert auth.calls == 1  # no retry on auth error


@pytest.mark.asyncio
async def test_fetch_offers_retries_5xx_then_succeeds():
    auth = _FailingAuthRest(status_code=503, succeed_after=3)
    rec = _boot_recovery(auth)
    result = await rec._fetch_offers("fUSD")
    assert result == []
    assert auth.calls == 3  # retried twice, succeeded on 3rd


@pytest.mark.asyncio
async def test_fetch_offers_reraises_after_transient_exhaustion():
    auth = _FailingAuthRest(status_code=0)  # transport error, never succeeds
    rec = _boot_recovery(auth)
    with pytest.raises(BitfinexAPIError):
        await rec._fetch_offers("fUSD")
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
    """Satisfies offers + credits + wallet-available queries."""
    def __init__(self, offers, credits, available=Decimal("0")):
        self._offers = offers
        self._credits = credits
        self._available = available

    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        return self._offers

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        return self._credits

    async def get_funding_available(self, *, ctx, currency):
        return self._available


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
    assert call["realized"] == Decimal("300")
    assert call["reserved"] == Decimal("0")
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


# ── Single-writer: registry routing (Task 5) ─────────────────────────────────


class _StubRegistry:
    def __init__(self):
        self.handled: list = []

    async def handle(self, event):
        self.handled.append(event)


@pytest.mark.asyncio
async def test_run_routes_recovery_actions_to_registry_not_bus():
    """With an offer_registry wired: PositionReconciled goes to the bus (ledger),
    recovery ReservationClaimed goes to the registry — NOT the bus. This prevents
    the double-count once the ledger subscribes to PositionReconciled."""
    registry = _StubRegistry()
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[_offer(voi="999", amount="200")], credits=[])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus, offer_registry=registry)

    await rec.run()

    # bus carries the snapshot signal only
    assert any(isinstance(e, PositionReconciled) for e in bus.published)
    assert not any(isinstance(e, ReservationClaimed) for e in bus.published)
    # registry receives the orphan claim (FSM)
    assert any(isinstance(e, ReservationClaimed) for e in registry.handled)


@pytest.mark.asyncio
async def test_run_falls_back_to_bus_when_no_registry():
    """No registry → recovery actions still reach the bus (legacy path)."""
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[_offer(voi="999", amount="200")], credits=[])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)  # no offer_registry

    await rec.run()

    assert any(isinstance(e, ReservationClaimed) for e in bus.published)


# ── Balance-aware cap gate: wallet available in reconcile pass (Task 4) ───────


@pytest.mark.asyncio
async def test_run_populates_available_from_wallets():
    """run() threads wallet available into ReconcileResult + published event."""
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRest(offers=[], credits=[], available=Decimal("147.5"))
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()

    assert result.available_usdt == Decimal("147.5")
    published = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert published and published[-1].available_usdt == Decimal("147.5")
    # available is NOT persisted: set_position_snapshot has no available_usdt kwarg.
    assert "available_usdt" not in store.snapshot_calls[-1]


@pytest.mark.asyncio
async def test_fetch_available_does_not_retry_4xx():
    class _FailingWallets:
        def __init__(self):
            self.calls = 0
        async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
            return []
        async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
            return []
        async def get_funding_available(self, *, ctx, currency):
            self.calls += 1
            raise BitfinexAPIError(status_code=401, message="boom")

    auth = _FailingWallets()
    rec = _boot_recovery(auth)
    with pytest.raises(BitfinexAPIError):
        await rec._fetch_available("fUSD")
    assert auth.calls == 1  # no retry on 4xx (fail-closed, same as offers/credits)


# ── Task 3D: thread offer.symbol + reconciler symbol into recovery actions ─────


def test_orphan_claimed_carries_offer_symbol() -> None:
    offer = ActiveFundingOffer(
        venue_offer_id="555", symbol="fUST", amount=Decimal("100"),
        rate=0.0003, period_days=2, mts_created=1_000_000, status="ACTIVE",
    )
    acts = compute_recovery_actions(
        venue_offers=[offer], local_claims=[], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000,
        configured_symbols=frozenset({"fUST"}),
    )
    assert isinstance(acts[0], ReservationClaimed) and acts[0].symbol == "fUST"


def test_missing_claim_released_carries_own_claim_symbol() -> None:
    claim = _claim(cid=42, voi="999", state=RegistryState.CLAIMED, size="80")
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim], account_id=_ACC,
        is_simulated=False, now_ms=_NOW, grace_ms=120_000,
        configured_symbols=frozenset({"fUST"}),
    )
    assert isinstance(acts[0], ReservationReleased) and acts[0].symbol == "fUST"


def test_compute_recovery_actions_requires_symbol() -> None:
    """fail-loud: configured_symbols is a required kwarg (no silent fUSD default)
    so a forgotten symbol set raises instead of mis-routing capital."""
    offer = ActiveFundingOffer(
        venue_offer_id="111", symbol="fUSD", amount=Decimal("50"),
        rate=0.0003, period_days=2, mts_created=1_000_000, status="ACTIVE",
    )
    with pytest.raises(TypeError):
        compute_recovery_actions(
            venue_offers=[offer], local_claims=[], account_id=_ACC,
            is_simulated=False, now_ms=_NOW, grace_ms=120_000,  # no symbols → TypeError
        )


def test_missing_release_uses_per_claim_symbol_not_primary():
    """fUSD is configured first (symbols[0]); a CLAIMED fUST offer missing from
    venue must release as fUST, not the primary fUSD."""
    scid = uuid4()
    claim = _claim(cid=42, voi="999", state=RegistryState.CLAIMED, size="80",
                   scid=scid, symbol="fUST")
    acts = compute_recovery_actions(
        venue_offers=[], local_claims=[claim],
        account_id="acct", is_simulated=False, now_ms=1_000,
        grace_ms=0, action_grace_ms=0,
        configured_symbols=frozenset({"fUSD", "fUST"}))
    assert len(acts) == 1
    ev = acts[0]
    assert isinstance(ev, ReservationReleased)
    assert ev.symbol == "fUST"          # per-claim, NOT symbols[0]=="fUSD"


def test_recovery_fails_loud_on_unconfigured_symbol():
    claim = _claim(cid=7, voi="7", state=RegistryState.CLAIMED, size="10",
                   scid=uuid4(), symbol="fXXX")
    with pytest.raises(ValueError):
        compute_recovery_actions(
            venue_offers=[], local_claims=[claim],
            account_id="acct", is_simulated=False, now_ms=1_000,
            grace_ms=0, action_grace_ms=0,
            configured_symbols=frozenset({"fUSD", "fUST"}))


# ── Per-symbol plumbing (Cluster D Task 1) ───────────────────────────────────


def _boot_recovery_symbols(auth_rest, store, session_factory, bus, *, symbols, **kw):
    """BootRecovery wired with the new `symbols` list arg (run() tests)."""
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
        symbols=symbols,
        **kw,
    )


@pytest.mark.asyncio
async def test_single_symbol_list_reproduces_current_event():
    """One configured symbol → exactly one PositionReconciled, identical natives."""
    offers = [_offer(voi="555", amount="100")]
    credits = [_credit("1", "200")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=offers, credits=credits, available=Decimal("47.5"))
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST"],
    )

    result = await rec.run()

    pr = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr) == 1
    assert pr[0].symbol == "fUST"
    assert pr[0].reserved == Decimal("100")
    assert pr[0].realized == Decimal("200")
    assert pr[0].available == Decimal("47.5")
    assert pr[0].n_offers == 1
    assert pr[0].n_credits == 1
    # result keeps the aggregate dims (single symbol == today)
    assert result.reserved_usdt == Decimal("100")
    assert result.realized_usdt == Decimal("200")
    assert result.available_usdt == Decimal("47.5")
    assert result.n_credits == 1


@pytest.mark.asyncio
async def test_legacy_symbol_kwarg_still_constructs_single_symbol():
    """Back-compat: passing the old `symbol=` kwarg yields a 1-element symbol list."""
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=[_credit("1", "150")])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus, symbol="fUST")

    await rec.run()

    pr = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr) == 1
    assert pr[0].symbol == "fUST"
    assert pr[0].realized == Decimal("150")


def test_boot_recovery_requires_symbol_or_symbols():
    """fail-loud: constructing with NEITHER symbol nor symbols raises (no silent
    fUSD default) so a forgotten currency can't silently reconcile fUSD."""
    auth = _StubAuthRestFull(offers=[], credits=[])
    with pytest.raises(ValueError):
        BootRecovery(
            store=_StubStore(),
            session_factory=_StubSessionFactory(),
            auth_rest=auth,
            account_ctx=AccountContext(
                account_id="default",
                credentials=Credentials(api_key="k", api_secret="s"),
                allocation_cap_usdt=Decimal("1"),
            ),
            deployment_environment="ci",
            bus=_StubBus(),
            max_attempts=1,
            backoff_base_s=0,
            clock=lambda: _NOW,
        )


# ── Multi-symbol reconcile loop (Cluster D Task 2) ───────────────────────────


def _offer_sym(symbol, voi, amount):
    return ActiveFundingOffer(
        venue_offer_id=voi, symbol=symbol, amount=Decimal(amount),
        rate=0.0003, period_days=2, mts_created=1_000_000, status="ACTIVE",
    )


def _credit_sym(symbol, credit_id, amount):
    return ActiveFundingCredit(
        credit_id=credit_id, symbol=symbol, amount=Decimal(amount),
        rate=0.0003, period_days=2, status="ACTIVE",
    )


class _StubAuthPerSymbol:
    """Per-symbol offers/credits/available keyed by symbol."""
    def __init__(self, offers_by_sym, credits_by_sym, available_by_sym):
        self._offers = offers_by_sym
        self._credits = credits_by_sym
        self._available = available_by_sym
        self.offer_calls: list[str] = []
        self.credit_calls: list[str] = []
        self.wallet_calls: list[str] = []

    async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
        self.offer_calls.append(symbol)
        return self._offers.get(symbol, [])

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        self.credit_calls.append(symbol)
        return self._credits.get(symbol, [])

    async def get_funding_available(self, *, ctx, currency):
        self.wallet_calls.append(currency)
        return self._available.get(currency, Decimal("0"))


@pytest.mark.asyncio
async def test_two_symbols_fire_two_position_reconciled_with_per_symbol_natives():
    auth = _StubAuthPerSymbol(
        offers_by_sym={
            "fUST": [_offer_sym("fUST", "1", "100")],
            "fUSD": [_offer_sym("fUSD", "2", "40")],
        },
        credits_by_sym={
            "fUST": [_credit_sym("fUST", "c1", "200")],
            "fUSD": [],
        },
        available_by_sym={"UST": Decimal("17.5"), "USD": Decimal("9")},
    )
    store = _StubStore()
    bus = _StubBus()
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST", "fUSD"],
    )

    await rec.run()

    pr = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert len(pr) == 2
    by_sym = {e.symbol: e for e in pr}
    assert by_sym["fUST"].reserved == Decimal("100")
    assert by_sym["fUST"].realized == Decimal("200")
    assert by_sym["fUST"].available == Decimal("17.5")
    assert by_sym["fUST"].n_offers == 1 and by_sym["fUST"].n_credits == 1
    assert by_sym["fUSD"].reserved == Decimal("40")
    assert by_sym["fUSD"].realized == Decimal("0")
    assert by_sym["fUSD"].available == Decimal("9")
    assert by_sym["fUSD"].n_offers == 1 and by_sym["fUSD"].n_credits == 0
    # one wallet read per symbol's currency (no cross-symbol sum)
    assert auth.wallet_calls == ["UST", "USD"]


@pytest.mark.asyncio
async def test_two_symbols_write_per_symbol_snapshot_rows():
    auth = _StubAuthPerSymbol(
        offers_by_sym={"fUST": [_offer_sym("fUST", "1", "100")], "fUSD": []},
        credits_by_sym={"fUST": [], "fUSD": [_credit_sym("fUSD", "c1", "55")]},
        available_by_sym={"UST": Decimal("1"), "USD": Decimal("2")},
    )
    store = _StubStore()
    bus = _StubBus()
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST", "fUSD"],
    )

    await rec.run()

    assert len(store.snapshot_calls) == 2
    by_sym = {c["symbol"]: c for c in store.snapshot_calls}
    assert by_sym["fUST"]["reserved"] == Decimal("100")
    assert by_sym["fUST"]["realized"] == Decimal("0")
    assert by_sym["fUSD"]["reserved"] == Decimal("0")
    assert by_sym["fUSD"]["realized"] == Decimal("55")


@pytest.mark.asyncio
async def test_multi_symbol_result_aggregates_dims():
    auth = _StubAuthPerSymbol(
        offers_by_sym={"fUST": [_offer_sym("fUST", "1", "100")], "fUSD": []},
        credits_by_sym={
            "fUST": [_credit_sym("fUST", "c1", "200")],
            "fUSD": [_credit_sym("fUSD", "c2", "30")],
        },
        available_by_sym={"UST": Decimal("5"), "USD": Decimal("7")},
    )
    store = _StubStore()
    bus = _StubBus()
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST", "fUSD"],
    )

    result = await rec.run()

    # aggregate across symbols (PeriodicReconcile divergence/drift unchanged)
    assert result.reserved_usdt == Decimal("100")
    assert result.realized_usdt == Decimal("230")
    assert result.available_usdt == Decimal("12")
    assert result.n_credits == 2


# --- from_snapshot loads each claim's own symbol (fUSD P2 Task 3) ------------

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from bfx_funding_bot.core.db import Base  # noqa: E402
from bfx_funding_bot.modules.execution.event_store.tables import (  # noqa: E402
    OfferClaimRow,
)
from bfx_funding_bot.modules.execution.registry_offers import (  # noqa: E402
    OfferRegistry,
)


async def _create_all(session: AsyncSession) -> None:
    bind = session.bind
    assert bind is not None
    async with bind.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)


async def test_from_snapshot_loads_symbol(sqlite_session: AsyncSession) -> None:
    await _create_all(sqlite_session)
    sqlite_session.add(OfferClaimRow(
        cid=42, account_id="acct", deployment_environment="ci",
        state="claimed", venue_offer_id="v42", symbol="fUST",
        size_usdt=Decimal("100"),
        signal_correlation_id="11111111-1111-1111-1111-111111111111",
        occurred_at_ms=1000, last_updated_ms=2000, last_event_seq=0))
    await sqlite_session.flush()
    reg = await OfferRegistry.from_snapshot(
        sqlite_session, account_id="acct", deployment_environment="ci")
    assert reg._snapshot["v42"].symbol == "fUST"
