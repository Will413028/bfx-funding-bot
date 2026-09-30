"""Pin the active observation wire digest independently of database storage."""

from dataclasses import replace
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.ledger import Coverage, Credit, Observation, Offer, Wallet
from bfx_funding_bot.modules.ledger._internal.observation import _validate, digest_bytes


def _coverage() -> Coverage:
    return Coverage(True, True, True, True, True, True, 1, 1, 1, 1, 0, 0)


def _offer(venue_id: str = "o1") -> Offer:
    return Offer(
        venue_id,
        "fUST",
        Decimal("2"),
        Decimal("1"),
        None,
        False,
        2,
        None,
        None,
        "ACTIVE",
        1,
        None,
        {},
    )


def _credit(kind: str = "loan") -> Credit:
    assert kind in ("credit", "loan")
    return Credit(kind, "7", "fUST", Decimal("1"), None, None, "ACTIVE", None, None, None, None, {})  # type: ignore[arg-type]


def test_digest_bytes_are_versioned_sorted_and_include_loans() -> None:
    first = Observation(
        (Wallet("funding", "UST", Decimal("3"), Decimal("9"), "fUST"),),
        (_offer("o2"), _offer("o1")),
        (_credit("loan"), _credit("credit")),
        _coverage(),
        5,
    )
    expected = (
        b'{"credits_and_loans":[{"amount":"1","flags":null,"mts_created":null,'
        b'"mts_opening":null,"mts_updated":null,"period_days":null,"rate":null,'
        b'"raw":{},"source_kind":"credit","status":"ACTIVE","symbol":"fUST",'
        b'"venue_credit_id":"7"},{"amount":"1","flags":null,"mts_created":null,'
        b'"mts_opening":null,"mts_updated":null,"period_days":null,"rate":null,'
        b'"raw":{},"source_kind":"loan","status":"ACTIVE","symbol":"fUST",'
        b'"venue_credit_id":"7"}],"domain":"bfx-ledger-observation-active","offers":['
        b'{"amount_original":"2","amount_remaining":"1","flags":null,'
        b'"mts_created":1,"mts_updated":null,"offer_type":null,"period_days":2,'
        b'"rate":null,"rate_observed":false,"raw":{},"status":"ACTIVE",'
        b'"symbol":"fUST","venue_offer_id":"o1"},{"amount_original":"2",'
        b'"amount_remaining":"1","flags":null,"mts_created":1,"mts_updated":null,'
        b'"offer_type":null,"period_days":2,"rate":null,"rate_observed":false,'
        b'"raw":{},"status":"ACTIVE","symbol":"fUST","venue_offer_id":"o2"}],'
        b'"version":1,"wallet_available":[["funding","UST","3"]]}'
    )
    assert digest_bytes(first) == expected
    reordered = replace(
        first, offers=tuple(reversed(first.offers)), credits=tuple(reversed(first.credits))
    )
    assert digest_bytes(reordered) == expected
    assert digest_bytes(replace(first, credits=(_credit("credit"),))) != expected
    assert (
        digest_bytes(replace(first, wallets=(replace(first.wallets[0], balance=Decimal("10")),)))
        == expected
    )
    assert (
        digest_bytes(
            replace(first, wallets=(replace(first.wallets[0], available=Decimal("3.00")),))
        )
        == expected
    )


def test_conflicting_duplicate_identity_is_rejected() -> None:
    observation = Observation((), (_offer(), replace(_offer(), status="OTHER")), (), _coverage(), 5)
    with pytest.raises(ValueError, match="conflicting"):
        digest_bytes(observation)


def test_loan_identity_uses_unprefixed_venue_id() -> None:
    observation = Observation(
        (), (), (replace(_credit(), venue_credit_id="loan:7"),), _coverage(), 5
    )
    with pytest.raises(ValueError, match="invalid credit"):
        _validate(observation)
