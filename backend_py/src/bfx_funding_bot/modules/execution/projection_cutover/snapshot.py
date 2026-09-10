"""Strict authority boundary for a newly queried complete account observation."""

from decimal import Decimal
from uuid import UUID

from bfx_funding_bot.modules.execution.events import VenueSnapshotObserved

from .contracts import Scope


def validate_cutover_snapshot(
    snapshot: VenueSnapshotObserved, *, scope: Scope, managed_symbols: frozenset[str],
    now_ms: int, max_age_ms: int,
) -> None:
    if (
        not isinstance(scope.account_id, UUID)
        or scope.environment not in {"ci", "shadow", "prod"}
        or snapshot.account_id != str(scope.account_id)
        or snapshot.environment != scope.environment
    ):
        raise ValueError("snapshot_scope_mismatch")
    if not managed_symbols or any(
        symbol not in {"fUST", "fUSD"} for symbol in managed_symbols
    ):
        raise ValueError("snapshot_managed_symbols_invalid")
    times = (now_ms, max_age_ms, snapshot.query_started_at_ms, snapshot.query_finished_at_ms)
    if any(type(value) is not int for value in times) or not (
        0 < max_age_ms <= 300_000
        and 0 <= snapshot.query_started_at_ms <= snapshot.query_finished_at_ms <= now_ms
        and now_ms - snapshot.query_started_at_ms <= max_age_ms
        and snapshot.occurred_at_ms == snapshot.query_finished_at_ms
    ):
        raise ValueError("snapshot_time_invalid")
    coverage = snapshot.coverage
    # The three full-account REST endpoints are unpaginated. A different page
    # count has no supported completeness proof in this collection boundary.
    if any(getattr(coverage, name) is not True for name in (
        "active_offers_complete", "active_credits_complete", "wallets_complete",
    )) or any(type(getattr(coverage, name)) is not int or getattr(coverage, name) != 1 for name in (
        "active_offer_pages", "active_credit_pages", "wallet_pages",
    )):
        raise ValueError("snapshot_coverage_incomplete")
    if set(snapshot.wallet_available) != managed_symbols:
        raise ValueError("snapshot_symbol_coverage_incomplete")
    if any(not value.is_finite() or value < 0 for value in snapshot.wallet_available.values()):
        raise ValueError("snapshot_wallet_unknown")
    for observations, id_field in ((snapshot.offers, "venue_offer_id"),
                                   (snapshot.credits, "credit_id")):
        ids: set[str] = set()
        for observation in observations:
            identity = getattr(observation, id_field)
            if identity in ids:
                raise ValueError("snapshot_duplicate_exposure")
            ids.add(identity)
            amount = getattr(observation, "amount_remaining", getattr(observation, "amount", None))
            rate = observation.rate
            if (
                observation.symbol not in managed_symbols
                or observation.status not in {"active", "partially_filled"}
                or not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0
                or not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0
                or type(observation.period_days) is not int or observation.period_days <= 0
                or type(observation.mts_created) is not int
                or type(observation.mts_updated) is not int
                or not 0 <= observation.mts_created <= observation.mts_updated
                <= snapshot.query_finished_at_ms
            ):
                raise ValueError("snapshot_unknown_exposure")
