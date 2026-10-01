from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.external.bitfinex.auth_ws import FccEvent, FcnEvent, FcuEvent, FocEvent
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    CreditClosed,
    OrderFilled,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    RegistryState,
)
from bfx_funding_bot.modules.execution.ws_dispatcher import translate_bfx_event


def _claim_record(voi: str = "v1", state: RegistryState = RegistryState.CLAIMED) -> ClaimRecord:
    scid = uuid4()
    return ClaimRecord(
        venue_offer_id=voi, cid=42, signal_correlation_id=scid,
        size_usdt=Decimal("100"), account_id="default",
        state=state, occurred_at_ms=1000, last_updated_ms=1000,
        reservation_ref=ReservationRef(
            execution_decision_id="d-ws", cid=42,
            signal_correlation_id=scid, venue_offer_id=voi,
        ),
    )


def _fcn(voi: str = "v1", credit_id: int = 999, raw_seq: int = 5) -> FcnEvent:
    return FcnEvent(
        credit_id=credit_id, symbol="fUSD", side=1,
        mts_create=2000, mts_update=2000,
        amount=Decimal("100"), rate=0.0005, period_days=2,
        raw_seq=raw_seq, raw=[],
    )


def _foc(voi: str = "v1", status: str = "CANCELED") -> FocEvent:
    return FocEvent(
        venue_offer_id=voi, symbol="fUSD",
        mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status=status,
        rate=0.0005, period_days=2, raw_seq=7, raw=[],
    )


def test_fcn_is_informational_no_op() -> None:
    """fcn no longer drives the lifecycle (it carries no offer id). foc EXECUTED
    is the fill signal; reconcile is the backbone. fcn → no domain effect."""
    snapshot = {"v1": _claim_record("v1")}
    events, mutations, _diags = translate_bfx_event(
        _fcn("v1", credit_id=999), snapshot, recent_cancels={}, now_ms=2500,
    )
    assert events == []
    assert mutations == []


def test_foc_canceled_with_recent_cancel_emits_user_cancel() -> None:
    snapshot = {"v1": _claim_record("v1")}
    recent_cancels = {"v1": 2000}
    events, _mutations, _ = translate_bfx_event(
        _foc("v1", status="CANCELED"), snapshot, recent_cancels, now_ms=2300,
    )
    assert len(events) == 1
    assert isinstance(events[0], ReservationReleased)
    assert events[0].reason == "user_cancel"


def test_foc_canceled_without_recent_cancel_emits_venue_cancel() -> None:
    snapshot = {"v1": _claim_record("v1")}
    events, _, _ = translate_bfx_event(
        _foc("v1", status="CANCELED"), snapshot, recent_cancels={}, now_ms=2500,
    )
    assert events[0].reason == "venue_cancel"


def test_foc_canceled_with_stale_cancel_emits_venue_cancel() -> None:
    """If cancel was > 5s ago, treat as venue-side cancel."""
    snapshot = {"v1": _claim_record("v1")}
    recent_cancels = {"v1": 1000}  # 6s ago
    events, _, _ = translate_bfx_event(
        _foc("v1", status="CANCELED"), snapshot, recent_cancels, now_ms=7001,
    )
    assert events[0].reason == "venue_cancel"


def test_foc_expired_emits_expired_reason() -> None:
    snapshot = {"v1": _claim_record("v1")}
    events, _, _ = translate_bfx_event(
        _foc("v1", status="EXPIRED"), snapshot, recent_cancels={}, now_ms=2500,
    )
    assert events[0].reason == "expired"


def test_foc_executed_on_claimed_emits_orderfilled() -> None:
    """foc EXECUTED is the authoritative fill signal (carries venue_offer_id)."""
    snapshot = {"v1": _claim_record("v1")}
    events, mutations, _diags = translate_bfx_event(
        _foc("v1", status="EXECUTED @ 0.0005 (100.0)"), snapshot, {}, now_ms=2500,
    )
    assert len(events) == 1
    assert isinstance(events[0], OrderFilled)
    assert events[0].credit_id is None        # foc carries no credit id
    assert events[0].venue_offer_id == "v1"
    assert events[0].fill_rate == 0.0005
    assert events[0].venue_seq == 7
    assert events[0].occurred_at_ms == 2000   # _foc mts_update
    assert events[0].reservation_ref.execution_decision_id == "d-ws"
    assert len(mutations) == 1
    assert mutations[0].new_state == RegistryState.RELEASED


def test_unmatched_foc_is_a_warning_not_a_correlation_error() -> None:
    # A foc with no claim is a foreign offer (manual, auto-renew) closing, or
    # one of ours the registry has not seen; periodic reconcile owns both.
    for status in ("EXECUTED @ 0.0005 (100.0)", "CANCELED"):
        events, mutations, diags = translate_bfx_event(
            _foc("unknown", status=status), {}, {}, now_ms=2500,
        )

        assert events == [] and mutations == []
        assert [d.level for d in diags] == ["warn"]
        assert "unmatched" in diags[0].message


def test_any_event_on_released_state_is_idempotent_no_op() -> None:
    snapshot = {"v1": _claim_record("v1", state=RegistryState.RELEASED)}
    events, mutations, _ = translate_bfx_event(
        _foc("v1", status="CANCELED"), snapshot, {}, now_ms=2500,
    )
    assert events == []
    assert mutations == []


def test_translate_is_pure_no_random_no_clock() -> None:
    snapshot = {"1": _claim_record("1")}
    out1 = translate_bfx_event(_fcn("v1"), snapshot, {}, now_ms=2000)
    out2 = translate_bfx_event(_fcn("v1"), snapshot, {}, now_ms=2000)
    assert out1 == out2


def test_fcu_event_is_no_op() -> None:
    """4.4a: FcuEvent (credit rate update) not modeled."""
    snapshot = {"v1": _claim_record("v1", state=RegistryState.RELEASED)}
    fcu = FcuEvent(
        credit_id=999, symbol="fUSD", mts_update=3000,
        amount=Decimal("100"), rate=0.0006, raw_seq=10, raw=[],
    )
    events, mutations, _ = translate_bfx_event(fcu, snapshot, {}, now_ms=3500)
    assert events == []
    assert mutations == []


def test_foc_executed_orderfilled_carries_foc_symbol() -> None:
    claim = _claim_record("v1")
    foc = FocEvent(
        venue_offer_id="v1", symbol="fUST", mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status="EXECUTED @ 0.0005 (100.0)", rate=0.0005,
        period_days=2, raw_seq=7, raw=[],
    )
    events, _, _ = translate_bfx_event(foc, {"v1": claim}, {}, now_ms=2500)
    assert isinstance(events[0], OrderFilled) and events[0].symbol == "fUST"


def test_foc_canceled_reservation_released_carries_foc_symbol() -> None:
    claim = _claim_record("v1")
    foc = FocEvent(
        venue_offer_id="v1", symbol="fUST", mts_create=1000, mts_update=2000,
        amount=Decimal("100"), status="CANCELED", rate=0.0005, period_days=2,
        raw_seq=8, raw=[],
    )
    events, _, _ = translate_bfx_event(foc, {"v1": claim}, {}, now_ms=2500)
    assert isinstance(events[0], ReservationReleased) and events[0].symbol == "fUST"


def test_fcc_emits_audit_only_credit_closed() -> None:
    """fcc → CreditClosed (attribution release truth). No registry mutation —
    credits are not offers; the ledger stays reconcile-owned."""
    fcc = FccEvent(
        credit_id=555, symbol="fUST", mts_create=1000, mts_update=9000,
        amount=Decimal("1338.03"), status="CLOSED (used)", rate=0.000174,
        period_days=2, raw_seq=11, raw=[], mts_opening=1000, mts_last_payout=8000,
    )
    events, mutations, diags = translate_bfx_event(
        fcc, snapshot={}, recent_cancels={}, now_ms=9500, account_id="primary",
    )
    assert mutations == [] and diags == []
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, CreditClosed)
    assert ev.credit_id == 555
    assert ev.symbol == "fUST"
    assert ev.amount == Decimal("1338.03")
    assert ev.mts_create == 1000
    assert ev.occurred_at_ms == 8000  # close time = mts_last_payout, not mts_update
    assert (ev.mts_opening, ev.mts_last_payout) == (1000, 8000)
    assert ev.venue_fields_reliable
    assert ev.account_id == "primary"
    assert ev.is_simulated is False
    assert ev.venue_seq == 11


def test_fcc_without_last_payout_falls_back_to_mts_update() -> None:
    fcc = FccEvent(
        credit_id=556, symbol="fUST", mts_create=1000, mts_update=9000,
        amount=Decimal("150"), status="CLOSED (used)", rate=0.0002,
        period_days=2, raw_seq=12, raw=[],
    )
    events, _, _ = translate_bfx_event(
        fcc, snapshot={}, recent_cancels={}, now_ms=9500, account_id="primary",
    )
    ev = events[0]
    assert isinstance(ev, CreditClosed)
    assert ev.occurred_at_ms == 9000 and not ev.venue_fields_reliable
