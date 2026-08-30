from decimal import Decimal
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.registry_offers import (
    ClaimRecord,
    RegistryState,
    transition,
)


def _ref(voi: str | None = None, *, cid: int = 42, decision_id: str = "d-1") -> ReservationRef:
    return ReservationRef(
        execution_decision_id=decision_id,
        cid=cid,
        signal_correlation_id=uuid4(),
        venue_offer_id=voi,
    )


def _claim(voi: str = "v1", size: int = 100, *, ref: ReservationRef | None = None) -> ReservationClaimed:
    reference = ref or _ref(voi)
    return ReservationClaimed(
        cid=42, venue_offer_id=voi, size_usdt=Decimal(size),
        signal_correlation_id=reference.signal_correlation_id, account_id="default",
        is_simulated=False, occurred_at_ms=1000,
        symbol="fUST", reservation_ref=reference)


def _filled(
    voi: str = "v1", venue_seq: int = 100, *, ref: ReservationRef | None = None,
) -> OrderFilled:
    reference = ref or _ref(voi)
    return OrderFilled(
        cid=42, venue_offer_id=voi, credit_id="C-1",
        size_usdt=Decimal("100"), fill_rate=0.0005,
        signal_correlation_id=reference.signal_correlation_id, account_id="default",
        is_simulated=False, venue_seq=venue_seq, occurred_at_ms=2000,
        symbol="fUST", reservation_ref=reference)


def _released(
    voi: str = "v1", reason: str = "user_cancel", *, ref: ReservationRef | None = None,
) -> ReservationReleased:
    reference = ref or _ref(voi)
    return ReservationReleased(
        cid=42, venue_offer_id=voi, size_usdt=Decimal("100"),
        reason=reason, signal_correlation_id=reference.signal_correlation_id,
        account_id="default", is_simulated=False, occurred_at_ms=3000,
        symbol="fUST", reservation_ref=reference)


def test_claim_on_empty_snapshot_adds_record() -> None:
    snap: dict[str, ClaimRecord] = {}
    new_snap, _diags = transition(snap, _claim(), now_ms=1000)
    assert "v1" in new_snap
    assert new_snap["v1"].state == RegistryState.CLAIMED
    assert new_snap["v1"].size_usdt == Decimal("100")


def test_duplicate_claim_no_op() -> None:
    snap: dict[str, ClaimRecord] = {}
    claim = _claim()
    snap, _ = transition(snap, claim, now_ms=1000)
    snap2, _ = transition(snap, claim, now_ms=1001)
    assert snap == snap2


def test_conflicting_duplicate_offer_id_is_fail_closed() -> None:
    snap, _ = transition({}, _claim(), now_ms=1000)
    _same_voi_different_correlation = _ref("v1", cid=42, decision_id="d-2")

    unchanged, diags = transition(
        snap, _claim(ref=_same_voi_different_correlation), now_ms=1001,
    )

    assert unchanged == snap
    assert diags[0].level == "error"
    assert "conflicting" in diags[0].message


def test_filled_on_claimed_transitions_to_released() -> None:
    snap: dict[str, ClaimRecord] = {}
    claim = _claim()
    snap, _ = transition(snap, claim, now_ms=1000)
    snap, _ = transition(snap, _filled(ref=claim.reservation_ref), now_ms=2000)
    assert snap["v1"].state == RegistryState.RELEASED


def test_filled_on_empty_yields_diagnostic_no_mutation() -> None:
    snap: dict[str, ClaimRecord] = {}
    new_snap, diags = transition(snap, _filled(), now_ms=2000)
    assert new_snap == {}
    assert len(diags) == 1
    assert "unmatched fill" in diags[0].message.lower()


def test_filled_on_released_idempotent() -> None:
    snap: dict[str, ClaimRecord] = {}
    claim = _claim()
    fill = _filled(ref=claim.reservation_ref)
    snap, _ = transition(snap, claim, now_ms=1000)
    snap, _ = transition(snap, fill, now_ms=2000)
    snap_before = dict(snap)
    snap, _ = transition(snap, fill, now_ms=2001)
    assert snap == snap_before


def test_released_on_claimed_transitions_to_released() -> None:
    snap: dict[str, ClaimRecord] = {}
    claim = _claim()
    snap, _ = transition(snap, claim, now_ms=1000)
    snap, _ = transition(snap, _released(ref=claim.reservation_ref), now_ms=3000)
    assert snap["v1"].state == RegistryState.RELEASED


def test_released_on_released_idempotent() -> None:
    snap: dict[str, ClaimRecord] = {}
    claim = _claim()
    release = _released(ref=claim.reservation_ref)
    snap, _ = transition(snap, claim, now_ms=1000)
    snap, _ = transition(snap, release, now_ms=3000)
    snap_before = dict(snap)
    snap, _ = transition(snap, release, now_ms=3001)
    assert snap == snap_before


def test_released_on_empty_emits_warn_diag_no_mutation() -> None:
    new_snap, diags = transition({}, _released(), now_ms=3000)
    assert new_snap == {}
    assert any("unmatched release" in d.message.lower() for d in diags)


@given(voi=st.text(min_size=1, max_size=10, alphabet="abcdefv0123456789"))
def test_transition_dict_size_never_decreases(voi: str) -> None:
    snap: dict[str, ClaimRecord] = {}
    claim = _claim(voi=voi)
    snap, _ = transition(snap, claim, now_ms=1000)
    snap_before_size = len(snap)
    snap, _ = transition(snap, _filled(voi=voi, ref=claim.reservation_ref), now_ms=2000)
    assert len(snap) >= snap_before_size


def test_transition_is_pure_no_clock_dependence() -> None:
    """Same input twice → same state (now_ms only affects last_updated_ms, not state)."""
    snap: dict[str, ClaimRecord] = {}
    out1, _ = transition(dict(snap), _claim(), now_ms=1000)
    out2, _ = transition(dict(snap), _claim(), now_ms=9999)
    assert out1["v1"].state == out2["v1"].state
    assert out1["v1"].venue_offer_id == out2["v1"].venue_offer_id


def test_unknown_event_type_emits_info_diag_no_mutation() -> None:
    class Bogus:
        venue_offer_id = "v1"

    snap: dict[str, ClaimRecord] = {}
    new_snap, diags = transition(snap, Bogus(), now_ms=1000)
    assert new_snap == snap
    assert len(diags) == 1
    assert diags[0].level == "info"
    assert "unknown event type" in diags[0].message.lower()


def test_event_missing_venue_offer_id_emits_warn_diag_no_mutation() -> None:
    class NoVOI:
        pass

    snap: dict[str, ClaimRecord] = {}
    new_snap, diags = transition(snap, NoVOI(), now_ms=1000)
    assert new_snap == snap
    assert len(diags) == 1
    assert diags[0].level == "warn"
    assert "venue_offer_id" in diags[0].message


def test_claim_record_carries_symbol_from_reservation_claimed() -> None:
    reference = _ref("voi-1", cid=10)
    ev = ReservationClaimed(
        cid=10, venue_offer_id="voi-1", size_usdt=Decimal("100"),
        signal_correlation_id=reference.signal_correlation_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000, symbol="fUST", reservation_ref=reference,
    )
    new_snapshot, _ = transition({}, ev, now_ms=2000)
    assert isinstance(new_snapshot["voi-1"], ClaimRecord)
    assert new_snapshot["voi-1"].symbol == "fUST"


def test_claim_record_carries_fusd_symbol_through_fsm() -> None:
    # symbol is mandatory on ReservationClaimed (Task 11); the FSM faithfully
    # carries whatever symbol the event sets — here the legacy fUSD currency.
    reference = _ref("voi-2", cid=11)
    ev = ReservationClaimed(
        cid=11, venue_offer_id="voi-2", size_usdt=Decimal("50"),
        signal_correlation_id=reference.signal_correlation_id, account_id="default", is_simulated=False,
        occurred_at_ms=1000, symbol="fUSD", reservation_ref=reference,
    )
    new_snapshot, _ = transition({}, ev, now_ms=2000)
    assert new_snapshot["voi-2"].symbol == "fUSD"
