"""Persisted events are versioned: upcast what is old, refuse what is unknown."""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.simulated_venue import events as ev
from bfx_funding_bot.modules.simulated_venue.events import (
    SCHEMA_VERSION,
    TradeTick,
    UnknownEventVersionError,
    event_from_payload,
    event_to_payload,
)

D = Decimal
SAMPLES = [
    ev.WalletFunded("UST", D("1000.5"), 1),
    ev.BookObserved("fUST", 5, ((D("0.0002"), 2, D("10")),), 6),
    ev.OfferPlaced(1, "fUST", D("150"), D("0.0002"), 2, 7, D("12")),
    ev.TradesObserved("fUST", 9, (TradeTick(8, D("3"), D("0.0002"), 2),), 9),
    ev.OfferFilled(1, 2, 3, D("5"), D("0.0002"), 2, 10),
    ev.LoanDrawn(3, 4, 11),
    ev.OfferCanceled(1, 12),
    ev.CreditClosed("loan", 3, 13, "expired"),
    ev.InterestPaid(5, "UST", D("0.01"), D("1000.51"), 14),
    ev.NonceAdvanced(99, 15),
    ev.FaultInjected("unknown_placed_lost", "submit", 3, 99, "fUST", D("150"), D("0.0002"), 2, 16),
    ev.FaultInjected("history_error", "history", 4, 100, None, None, None, None, 17),
    ev.InternalFailureRecorded("feed", "feed trades failed", 18),
    ev.UnexpectedRequestRecorded("GET", "https://example.test/x", 19),
]


def test_every_event_type_is_covered_by_a_sample() -> None:
    assert {type(e).event_type for e in SAMPLES} == set(ev._EVENTS)


@pytest.mark.parametrize("event", SAMPLES, ids=lambda e: f"{type(e).event_type}-{e.mts}")
def test_payload_carries_stable_type_and_version_and_round_trips(event: ev.VenueEvent) -> None:
    payload = event_to_payload(event)
    assert payload["event_type"] == type(event).event_type
    assert payload["schema_version"] == SCHEMA_VERSION == event.schema_version
    assert "TradeTick" not in repr(payload) and "OfferPlaced" not in repr(payload)  # no class names
    assert event_from_payload(copy.deepcopy(payload)) == event


def test_unknown_versions_types_and_garbage_are_refused() -> None:
    good = event_to_payload(SAMPLES[0])
    for bad in (
        {**good, "schema_version": SCHEMA_VERSION + 1},       # written by a newer venue
        {**good, "schema_version": 0},                         # no upcaster registered
        {**good, "schema_version": "1"},
        {**good, "schema_version": True},
        {**good, "event_type": "WalletFunded"},                # a class name is not the contract
        {**good, "event_type": "nope"},
        {"data": good["data"]},
        {"$t": "WalletFunded", "currency": "UST"},             # the round-1 format
    ):
        with pytest.raises(UnknownEventVersionError):
            event_from_payload(bad)


def test_an_old_version_is_upcast_step_by_step(monkeypatch: pytest.MonkeyPatch) -> None:
    # A future schema 3 whose upcasters rewrite the amount at each hop: a v1 payload must
    # pass through v1->v2 and v2->v3, a v2 payload only through v2->v3.
    monkeypatch.setitem(ev._VERSIONS, "wallet_funded", 3)
    hops: list[int] = []

    def to_v2(data: dict) -> dict:  # type: ignore[type-arg]
        hops.append(1)
        return {**data, "amount": {"$dec": "20"}}

    def to_v3(data: dict) -> dict:  # type: ignore[type-arg]
        hops.append(2)
        return {**data, "amount": {"$dec": str(Decimal(data["amount"]["$dec"]) + 1)}}

    monkeypatch.setitem(ev._UPCASTERS, ("wallet_funded", 1), to_v2)
    monkeypatch.setitem(ev._UPCASTERS, ("wallet_funded", 2), to_v3)
    old = {**event_to_payload(SAMPLES[0]), "schema_version": 1}
    assert event_from_payload(old).amount == D("21") and hops == [1, 2]
    hops.clear()
    assert event_from_payload({**old, "schema_version": 2}).amount == D("1001.5")
    assert hops == [2]
    with pytest.raises(UnknownEventVersionError):  # v0 has no upcaster
        event_from_payload({**old, "schema_version": 0})
    with pytest.raises(UnknownEventVersionError):  # newer than this reader
        event_from_payload({**old, "schema_version": 4})


def test_versions_belong_to_the_type_and_every_type_is_still_v1() -> None:
    """Adding a type must not move the version of the log that already exists."""
    assert set(ev._VERSIONS) == set(ev._EVENTS) and set(ev._VERSIONS.values()) == {1}
    assert all(event_to_payload(e)["schema_version"] == 1 for e in SAMPLES)
    assert ev._UPCASTERS == {}


def test_a_v1_only_log_written_now_decodes_with_the_type_table_of_before_the_new_types(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader that predates the record types reads every older row and refuses the new ones."""
    new_types = {"fault_injected", "internal_failure_recorded", "unexpected_request_recorded"}
    monkeypatch.setattr(ev, "_EVENTS", {k: v for k, v in ev._EVENTS.items() if k not in new_types})
    for event in SAMPLES:
        payload = event_to_payload(event)
        if payload["event_type"] in new_types:
            with pytest.raises(UnknownEventVersionError):
                event_from_payload(copy.deepcopy(payload))
        else:
            assert payload["schema_version"] == 1
            assert event_from_payload(copy.deepcopy(payload)) == event


def test_a_type_nobody_knows_is_refused_whatever_its_version() -> None:
    for version in (1, 2):
        with pytest.raises(UnknownEventVersionError):
            event_from_payload({"event_type": "from_the_future", "schema_version": version,
                                "data": {}})


def test_a_fault_injection_keeps_its_kind_target_ordinal_content_and_time() -> None:
    payload = event_to_payload(SAMPLES[10])
    assert payload["data"] == {
        "fault_kind": "unknown_placed_lost", "target": "submit", "request_ordinal": 3,
        "nonce": 99, "symbol": "fUST", "amount": {"$dec": "150"}, "rate": {"$dec": "0.0002"},
        "period": 2, "mts": 16}
