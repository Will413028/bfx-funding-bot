from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.execution.boot_recovery import (
    BootRecovery,
    LocalClaim,
    ReconcileResult,
    compute_recovery_actions,
)
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    ReservationReleased,
    ReservationUnknown,
    VenueOfferQuarantined,
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
    correlation_id = scid or uuid4()
    return LocalClaim(
        cid=cid, venue_offer_id=voi, state=state, size_usdt=Decimal(size),
        signal_correlation_id=correlation_id, occurred_at_ms=occurred,
        symbol=symbol,
        reservation_ref=ReservationRef(
            execution_decision_id=f"d-recovery-{cid}", cid=cid,
            signal_correlation_id=correlation_id, venue_offer_id=voi,
        ),
    )


def _actions(venue, local, grace_ms=120_000, now=_NOW):
    return compute_recovery_actions(
        venue_offers=venue, local_claims=local, account_id=_ACC,
        is_simulated=False, now_ms=now, grace_ms=grace_ms,
        configured_symbols=frozenset({"fUSD", "fUST"}),
    )


def test_unclaimed_venue_offer_produces_no_recovery_action():
    """D2: an offer no claim traces to is foreign (or an UNKNOWN's candidate),
    never a quarantine breadcrumb and never a synthetic claim."""
    assert _actions([_offer(voi="555", amount="250")], []) == []


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
    assert isinstance(ev, ReservationUnknown)
    assert ev.cid == 7 and ev.size_usdt == Decimal("60")
    assert ev.reason == "unresolved_at_boot" and ev.signal_correlation_id == scid


def test_recent_pending_within_grace_is_left_alone():
    claim = _claim(cid=7, voi=None, state=RegistryState.PENDING,
                   occurred=_NOW - 1_000)  # within grace
    assert _actions([], [claim], grace_ms=120_000) == []


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


class _StubStore:
    """Minimal PostgresEventStore stub — records events, no DB."""
    def __init__(self):
        self.appended: list = []
        self.snapshot_calls: list = []

    async def append(self, session, event):
        self.appended.append(event)
        return True

    async def append_snapshot(self, session, event):
        from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift
        self.snapshot_calls.append(event)
        await self.append(session, event)
        return SnapshotDrift(
            reserved_drift=Decimal("0"), realized_drift=Decimal("0"), event_seq=17
        )


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

    async def get_active_funding_loans(self, **kwargs):
        # Lent but not yet drawn into a position; none in this fixture.
        return []

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
async def test_run_reports_an_unclaimed_offer_as_unmanaged_without_quarantine():
    """run() names the offer unmanaged instead of inventing a claim identity or
    writing a quarantine breadcrumb (D2)."""
    # _StubSession returns no rows: no local claim, no attempt.
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRest([_offer(voi="999", amount="200")])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()
    assert result.unmanaged_offer_ids == frozenset({"999"})
    assert not any(isinstance(event, VenueOfferQuarantined) for event in store.appended)


@pytest.mark.asyncio
async def test_run_routes_unknown_to_symbol_gate_handler() -> None:
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRest([])
    seen: list[ReservationUnknown] = []

    async def mark_unknown(event: ReservationUnknown) -> None:
        seen.append(event)

    rec = _full_boot_recovery(
        auth,
        store,
        _StubSessionFactory(),
        bus,
        symbol="fUST",
        grace_ms=0,
        uncertainty_handler=mark_unknown,
    )

    async def stale_pending(_session) -> list[LocalClaim]:
        return [_claim(
            cid=88,
            voi=None,
            state=RegistryState.PENDING,
            size="125",
            occurred=_NOW - 1,
            symbol="fUST",
        )]

    rec._load_local_claims = stale_pending  # type: ignore[method-assign]
    result = await rec.run()

    assert result.n_unknown == 1
    assert seen and seen[0].cid == 88
    assert seen[0].symbol == "fUST"


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

    async def get_active_funding_loans(self, **kwargs):
        # Lent but not yet drawn into a position; none in this fixture.
        return []

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
    pr = next(e for e in pr_events if e.symbol == "fUST")
    assert pr.realized_usdt == Decimal("450")
    assert pr.reserved_usdt == Decimal("0")
    assert pr.n_credits == 3
    assert pr.n_offers == 0


@pytest.mark.asyncio
async def test_run_appends_full_account_snapshot_with_credit_sum():
    """run() appends one immutable observation with absolute venue values."""
    credits = [_credit("1", "300")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[], credits=credits)
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()

    assert len(store.snapshot_calls) == 1
    assert result.snapshot_event_seq == 17
    snapshot = store.snapshot_calls[0]
    assert sum((credit.amount for credit in snapshot.credits), Decimal("0")) == Decimal("300")
    assert snapshot.offers == ()
    assert snapshot.coverage.active_credits_complete is True


@pytest.mark.asyncio
async def test_run_preserves_scalar_venue_flags_in_snapshot() -> None:
    offer = ActiveFundingOffer(
        venue_offer_id="flagged",
        symbol="fUST",
        amount=Decimal("5"),
        rate=0.0003,
        period_days=2,
        mts_created=1_000_000,
        status="ACTIVE",
        flags=7,
    )
    store = _StubStore()
    bus = _StubBus()
    rec = _full_boot_recovery(
        _StubAuthRestFull(offers=[offer], credits=[]),
        store,
        _StubSessionFactory(),
        bus,
    )

    await rec.run()

    assert store.snapshot_calls[0].offers[0].flags == {"raw": 7}


@pytest.mark.asyncio
async def test_run_position_reconciled_includes_offer_reserved():
    """reserved = Σ(active offers) from venue, not from event accumulation."""
    offers = [_offer(voi="555", amount="100")]
    credits = [_credit("1", "200")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=offers, credits=credits)
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)

    result = await rec.run()
    assert len(result.unmanaged_offer_ids) == 1
    pr = next(e for e in bus.published if isinstance(e, PositionReconciled) and e.symbol == "fUSD")
    assert pr.reserved == Decimal("100")


@pytest.mark.asyncio
async def test_run_credits_fetch_failure_raises():
    """Credits fetch failure at boot → fail-fast (same as offers-fetch failure)."""
    class _FailCredits:
        async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
            return []

        async def get_active_funding_loans(self, **kwargs):
            # Lent but not yet drawn into a position; none in this fixture.
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
    """ReconcileResult carries drift returned by the snapshot event append."""
    from bfx_funding_bot.modules.execution.event_store.store import SnapshotDrift

    class _DriftStore(_StubStore):
        async def append_snapshot(self, session, event):
            await super().append_snapshot(session, event)
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

    result = await rec.run()
    assert len(result.unmanaged_offer_ids) == 1
    assert registry.handled == []
    assert registry.handled == []


@pytest.mark.asyncio
async def test_run_falls_back_to_bus_when_no_registry():
    """No registry → recovery actions still reach the bus (legacy path)."""
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=[_offer(voi="999", amount="200")], credits=[])
    rec = _full_boot_recovery(auth, store, _StubSessionFactory(), bus)  # no offer_registry

    result = await rec.run()
    assert len(result.unmanaged_offer_ids) == 1
    assert not any(isinstance(event, VenueOfferQuarantined) for event in bus.published)


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
    assert store.snapshot_calls[-1].wallet_available == {"fUSD": Decimal("147.5")}


@pytest.mark.asyncio
async def test_fetch_available_does_not_retry_4xx():
    class _FailingWallets:
        def __init__(self):
            self.calls = 0
        async def get_active_funding_offers(self, *, ctx, symbol="fUSD"):
            return []
        async def get_active_funding_loans(self, **kwargs):
            # Lent but not yet drawn into a position; none in this fixture.
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
    """A CLAIMED fUST offer missing from venue must release as fUST — the claim's
    own symbol — even when fUSD is also configured (no global primary-symbol stamp)."""
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
    assert ev.symbol == "fUST"          # the claim's own symbol, not a global stamp


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
    """Full-account reconcile emits configured and observed symbols."""
    offers = [_offer(voi="555", amount="100")]
    credits = [_credit("1", "200")]
    store = _StubStore()
    bus = _StubBus()
    auth = _StubAuthRestFull(offers=offers, credits=credits, available=Decimal("47.5"))
    rec = _boot_recovery_symbols(
        auth, store, _StubSessionFactory(), bus, symbols=["fUST"],
    )

    result = await rec.run()
    assert len(result.unmanaged_offer_ids) == 1
    published = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert {e.symbol for e in published} == {"fUSD", "fUST"}


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
        if symbol is None:
            return [offer for rows in self._offers.values() for offer in rows]
        return self._offers.get(symbol, [])

    async def get_active_funding_loans(self, **kwargs):
        # Lent but not yet drawn into a position; none in this fixture.
        return []

    async def get_active_funding_credits(self, *, ctx, symbol="fUSD"):
        self.credit_calls.append(symbol)
        if symbol is None:
            return [credit for rows in self._credits.values() for credit in rows]
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

    result = await rec.run()
    assert len(result.unmanaged_offer_ids) == 2
    published = [e for e in bus.published if isinstance(e, PositionReconciled)]
    assert {e.symbol for e in published} == {"fUSD", "fUST"}
    assert auth.wallet_calls == ["UST", "USD"]


@pytest.mark.asyncio
async def test_two_symbols_write_one_full_account_snapshot():
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

    result = await rec.run()
    assert len(result.unmanaged_offer_ids) == 1
    assert len(store.snapshot_calls) == 1
    snapshot = store.snapshot_calls[0]
    assert {offer.symbol for offer in snapshot.offers} == {"fUST"}
    assert {credit.symbol for credit in snapshot.credits} == {"fUSD"}


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
    assert len(result.unmanaged_offer_ids) == 1
    assert result.reserved_usdt == Decimal("100")
    assert result.realized_usdt == Decimal("230")


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


@pytest.mark.asyncio
async def test_boot_recovery_through_observation_port():
    from bfx_funding_bot.modules.execution.observation_sink import LegacyObservationSink
    from bfx_funding_bot.modules.ledger import Scope

    rec = _full_boot_recovery(_StubAuthRest([]), _StubStore(), _StubSessionFactory(), _StubBus())
    scope = Scope(uuid4(), "ci")
    result = await LegacyObservationSink(rec, scope).run(scope)
    assert result.decision == "accepted"
    assert isinstance(result.legacy, ReconcileResult)
    assert (result.legacy.n_claimed, result.legacy.n_released, result.legacy.n_failed) == (0, 0, 0)
