from bfx_funding_bot.modules.execution.event_upcasters import (
    UPCASTER_CHAIN,
    upcast_row,
    upcast_v1_to_v2,
)


def test_upcast_v1_to_v2_adds_optional_fields_as_none() -> None:
    v1 = {"event_type": "ORDER_FILL", "cid": 123, "venue_offer_id": "v1"}
    v2 = upcast_v1_to_v2(v1)
    assert v2["venue_seq"] is None
    assert v2["event_seq"] is None
    assert v2["occurred_at_ms"] is None
    assert v2["recorded_at_ms"] is None
    assert v2["event_type"] == "ORDER_FILL"
    assert v2["cid"] == 123
    assert v2["venue_offer_id"] == "v1"


def test_upcast_v1_to_v2_preserves_existing_field_values() -> None:
    """If row already has venue_seq etc (4.4a row), do not overwrite."""
    v_with_seq = {"event_type": "ORDER_FILL", "venue_seq": 42, "event_seq": 7}
    out = upcast_v1_to_v2(v_with_seq)
    assert out["venue_seq"] == 42
    assert out["event_seq"] == 7


def test_upcaster_chain_is_idempotent() -> None:
    v1 = {"event_type": "ORDER_FILL", "cid": 1}
    once = upcast_row(v1)
    twice = upcast_row(upcast_row(v1))
    assert once == twice


def test_upcast_row_runs_full_chain() -> None:
    v1 = {"event_type": "RESERVATION_CLAIMED", "cid": 99}
    out = upcast_row(v1)
    for field in ("venue_seq", "event_seq", "occurred_at_ms", "recorded_at_ms"):
        assert field in out


def test_chain_has_known_upcasters() -> None:
    assert upcast_v1_to_v2 in UPCASTER_CHAIN
