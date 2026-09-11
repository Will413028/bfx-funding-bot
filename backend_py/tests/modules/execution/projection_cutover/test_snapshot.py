"""Complete snapshot authority requires explicit scope, dimensions and freshness."""

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from bfx_funding_bot.modules.execution.projection_cutover.contracts import Scope

SCOPE = Scope(UUID(int=100), "ci")
SYMBOLS = frozenset({"fUST", "fUSD"})


def snapshot(**changes):
    return replace(VenueSnapshotObserved(
        account_id=str(SCOPE.account_id), environment="ci",
        query_started_at_ms=1000, query_finished_at_ms=1100,
        offers=(), credits=(), wallet_available={"fUST": Decimal("0"), "fUSD": Decimal("2")},
        coverage=SnapshotCoverage(True, True, True),
    ), **changes)


def validate(value, **changes):
    from bfx_funding_bot.modules.execution.projection_cutover import snapshot as module

    module.validate_cutover_snapshot(value, **{
        "scope": SCOPE, "managed_symbols": SYMBOLS, "now_ms": 1200,
        "max_age_ms": 300_000, **changes,
    })


def test_complete_empty_offers_and_explicit_zero_wallet_pass():
    validate(snapshot())


def test_preflight_accepts_stale_content_but_full_validation_still_rejects():
    from bfx_funding_bot.modules.execution.projection_cutover import snapshot as module

    value = snapshot()
    module.validate_cutover_snapshot_preflight(value, scope=SCOPE, managed_symbols=SYMBOLS)
    with pytest.raises(ValueError, match="snapshot_time_invalid"):
        validate(value, now_ms=301001)


@pytest.mark.parametrize("changes", [
    {"account_id": str(UUID(int=101))}, {"account_id": "not-a-uuid"},
    {"environment": "prod"}, {"wallet_available": {"fUST": Decimal("1")}},
    {"wallet_available": {"fUST": Decimal("1"), "fUSD": Decimal("0"), "fEUR": Decimal("0")}},
    {"query_started_at_ms": -1}, {"query_finished_at_ms": 1300},
    {"coverage": SnapshotCoverage(False, True, True)},
    {"coverage": SnapshotCoverage(True, False, True)},
    {"coverage": SnapshotCoverage(True, True, False)},
    {"coverage": SnapshotCoverage(True, True, True, active_offer_pages=2)},
    {"coverage": SnapshotCoverage(True, True, True, active_credit_pages=2)},
    {"coverage": SnapshotCoverage(True, True, True, wallet_pages=2)},
    {"wallet_available": {"fUST": Decimal("Infinity"), "fUSD": Decimal("0")}},
])
def test_invalid_snapshot_refuses_authority(changes):
    with pytest.raises(ValueError):
        validate(snapshot(**changes))


@pytest.mark.parametrize("changes", [
    {"now_ms": 301001}, {"max_age_ms": 0}, {"max_age_ms": 300001},
    {"managed_symbols": frozenset()}, {"now_ms": True},
])
def test_freshness_counts_from_query_start_and_cannot_relax_limit(changes):
    with pytest.raises(ValueError):
        validate(snapshot(), **changes)


def test_reversed_query_times_rejected_by_existing_value_contract():
    with pytest.raises(ValueError):
        snapshot(query_started_at_ms=1200)


@pytest.mark.parametrize("changes", [
    {"symbol": "fEUR"}, {"status": "unknown"}, {"rate": None},
    {"rate": Decimal("Infinity")}, {"period_days": None}, {"mts_updated": 1300},
])
def test_unknown_offer_exposure_rejected(changes):
    from bfx_funding_bot.modules.execution.events import VenueOfferObservation

    offer = VenueOfferObservation(
        venue_offer_id="1", symbol="fUST", amount_original=Decimal("2"),
        amount_remaining=Decimal("1"), rate=Decimal("0.001"), period_days=2,
        status="ACTIVE", mts_created=900, mts_updated=1000,
    )
    with pytest.raises(ValueError):
        validate(snapshot(offers=(replace(offer, **changes),)))


def test_infinite_original_offer_amount_cannot_establish_cutover_authority():
    from bfx_funding_bot.modules.execution.events import VenueOfferObservation

    offer = VenueOfferObservation(
        venue_offer_id="1", symbol="fUST", amount_original=Decimal("Infinity"),
        amount_remaining=Decimal("1"), rate=Decimal("0.001"), period_days=2,
        status="ACTIVE", mts_created=900, mts_updated=1000,
    )
    with pytest.raises(ValueError, match="snapshot_unknown_exposure"):
        validate(snapshot(offers=(offer,)))


def test_snapshot_cannot_backdate_its_event_time():
    with pytest.raises(ValueError):
        validate(snapshot(occurred_at_ms=999))


def test_duplicate_credit_identity_does_not_double_count_exposure():
    from bfx_funding_bot.modules.execution.event_store.entities import VenueCreditObservation

    credit = VenueCreditObservation("1", "fUST", Decimal("2"), Decimal("0.001"), 2, "ACTIVE", 900, 1000)
    validate(snapshot(credits=(credit,)))
    with pytest.raises(ValueError):
        validate(snapshot(credits=(credit, credit)))
