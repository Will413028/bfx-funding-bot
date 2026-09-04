"""Boot-time venue reconciliation (Phase 4.4c / 3a-recovery).

The venue (Bitfinex) is the ultimate truth for which funding offers exist;
the PG event_log + snapshot is the SoT for our intent + accounting. Bitfinex
funding offers carry NO client cid (submit drops it, response lacks it), so we
reconcile by venue_offer_id -- the only stable shared key.

  - orphan  (venue has voi, local has no CLAIMED row)  -> VenueOfferQuarantined
  - missing (local CLAIMED, venue no longer has voi)   -> ReservationReleased
  - stale PENDING (write-ahead intent, outcome unknown) -> ReservationUnknown

PENDING cannot be treated as rejection just because an active snapshot is empty.
It remains UNKNOWN until a fresh full-account/history observation or an
operator resolution proves what happened.  An unattributed venue offer is
counted and quarantined without a synthetic local identity or auto-cancel.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NamedTuple, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.external.bitfinex.auth_rest import (
    ActiveFundingCredit,
    ActiveFundingOffer,
    FundingOfferHistory,
    FundingOfferHistoryCoverage,
)
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError, BitfinexShapeError
from bfx_funding_bot.modules.accounts.exchange_accounts import account_scope_clause
from bfx_funding_bot.modules.execution.contracts import ReservationRef
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    VenueOfferObservation,
    is_terminal_offer_status,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.execution.events import (
    PositionReconciled,
    ReservationClaimed,
    ReservationFailed,
    ReservationReleased,
    ReservationUnknown,
    SnapshotCoverage,
    SubmitMatchedToVenueOffer,
    VenueOfferQuarantined,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.registry_offers import RegistryState
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeKind
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.execution.unknown_matching import (
    UnknownSubmitAttempt,
    attempt_from_row,
    match_unknown_attempt,
)
from bfx_funding_bot.modules.execution.unknown_matching import (
    offer_matches_attempt_identity as _offer_matches_attempt_identity,
)

log = logging.getLogger(__name__)

RecoveryAction = (
    ReservationClaimed
    | ReservationReleased
    | ReservationFailed
    | ReservationUnknown
    | VenueOfferQuarantined
)


def _normalize_flags(value: Mapping[str, Any] | int | None) -> Mapping[str, Any]:
    """Keep Bitfinex's scalar bitfield instead of silently dropping metadata."""
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    return {"raw": value}


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    n_claimed: int
    n_released: int
    n_failed: int
    reserved_usdt: Decimal = Decimal("0")
    realized_usdt: Decimal = Decimal("0")
    available_usdt: Decimal = Decimal("0")
    n_credits: int = 0
    reserved_drift_usdt: Decimal = Decimal("0")
    realized_drift_usdt: Decimal = Decimal("0")
    venue_offers: tuple[ActiveFundingOffer, ...] = ()
    n_unknown: int = 0
    n_quarantined: int = 0
    n_matched: int = 0
    snapshot_event_seq: int | None = None


class _SymbolSnapshot(NamedTuple):
    symbol: str
    offers: list[ActiveFundingOffer]
    credits: list[ActiveFundingCredit]
    available: Decimal
    reserved: Decimal
    realized: Decimal


@dataclass(frozen=True, slots=True)
class LocalClaim:
    cid: int
    venue_offer_id: str | None
    state: RegistryState
    size_usdt: Decimal
    signal_correlation_id: UUID
    occurred_at_ms: int
    symbol: str
    reservation_ref: ReservationRef | None = None


class RecoveryCorrelationError(RuntimeError):
    """Recovery cannot safely emit a lifecycle event without audited identity."""


async def convert_pending_to_unknown(
    session: AsyncSession,
    *,
    account_id: str,
    environment: str,
    now_ms: int,
) -> int:
    """Durably turn every unresolved PENDING claim into UNKNOWN once.

    This is the explicit cutover counterpart of boot recovery's stale-PENDING
    branch.  It performs no venue or executor work: the serialized event
    writer appends ``ReservationUnknown`` and projects its uncertainty in the
    same account transaction.  A rerun sees UNKNOWN rather than PENDING, so it
    appends neither a second outcome nor a second uncertainty.
    """
    try:
        canonical = UUID(account_id)
    except ValueError as exc:
        raise ValueError("PENDING conversion requires a canonical account UUID") from exc
    pending = (
        await session.execute(
            select(OfferClaimRow).where(
                OfferClaimRow.exchange_account_id == canonical,
                OfferClaimRow.deployment_environment == environment,
                OfferClaimRow.state == RegistryState.PENDING.value,
            ).order_by(OfferClaimRow.last_event_seq.asc(), OfferClaimRow.cid.asc())
        )
    ).scalars().all()
    events: list[object] = []
    for claim in pending:
        if claim.execution_decision_id is None:
            raise RecoveryCorrelationError(
                f"unresolved PENDING cid={claim.cid} has no reservation decision id"
            )
        try:
            signal_id = UUID(str(claim.signal_correlation_id))
        except ValueError as exc:
            raise RecoveryCorrelationError(
                f"unresolved PENDING cid={claim.cid} has invalid signal correlation"
            ) from exc
        events.append(ReservationUnknown(
            symbol=claim.symbol,
            cid=claim.cid,
            signal_correlation_id=signal_id,
            account_id=str(canonical),
            is_simulated=False,
            reason="unresolved_at_halt2_cutover",
            amount=Decimal(str(claim.size_usdt)),
            occurred_at_ms=now_ms,
            reservation_ref=ReservationRef(
                execution_decision_id=claim.execution_decision_id,
                cid=claim.cid,
                signal_correlation_id=signal_id,
            ),
        ))
    if not events:
        return 0
    await AccountEventWriter(
        store=PostgresEventStore(deployment_environment=environment)
    ).append_batch(session, events)
    return len(events)


def quarantine_orphan_offer(
    observation: ActiveFundingOffer,
    *,
    account_id: str,
    is_simulated: bool,
    observed_at_ms: int,
) -> VenueOfferQuarantined:
    """Create an audit-only orphan event without manufacturing provenance."""
    return VenueOfferQuarantined(
        venue_offer_id=observation.venue_offer_id,
        symbol=observation.symbol,
        amount=observation.amount,
        account_id=account_id,
        is_simulated=is_simulated,
        observed_at_ms=observed_at_ms,
        reason="unattributed_active_offer",
    )


def compute_recovery_actions(
    *,
    venue_offers: list[ActiveFundingOffer],
    local_claims: list[LocalClaim],
    account_id: str,
    is_simulated: bool,
    now_ms: int,
    grace_ms: int,
    action_grace_ms: int = 0,
    configured_symbols: frozenset[str],
) -> list[RecoveryAction]:
    """Pure reconciliation: produce the ordered list of domain events to append.

    action_grace_ms gates the orphan/missing directions so a runtime reconcile
    never acts on an offer/claim still mid-placement (boot passes 0 = immediate).
    """
    venue_by_voi = {o.venue_offer_id: o for o in venue_offers}
    claimed_by_voi = {
        c.venue_offer_id: c
        for c in local_claims
        if c.state == RegistryState.CLAIMED and c.venue_offer_id is not None
    }
    actions: list[RecoveryAction] = []

    # orphan: venue has it, local CLAIMED set doesn't -> quarantine.  The full
    # snapshot already persists/counts the object; fabricating a CID or
    # ReservationRef would create false provenance and make a later rebuild
    # impossible to audit.
    for voi, offer in venue_by_voi.items():
        if voi in claimed_by_voi:
            continue
        if (now_ms - offer.mts_created) < action_grace_ms:
            continue  # too fresh — local claim may still be committing
        actions.append(quarantine_orphan_offer(
            offer,
            account_id=account_id,
            is_simulated=is_simulated,
            observed_at_ms=now_ms,
        ))

    # missing: local CLAIMED, venue gone -> release (reserved -= size)
    for voi, claim in claimed_by_voi.items():
        if voi in venue_by_voi:
            continue
        if (now_ms - claim.occurred_at_ms) < action_grace_ms:
            continue  # too fresh — venue snapshot may lag the just-placed offer
        if claim.symbol not in configured_symbols:
            raise ValueError(
                f"recovery release for cid={claim.cid} has symbol={claim.symbol!r} "
                f"not in configured {sorted(configured_symbols)}")
        if claim.reservation_ref is None:
            raise RecoveryCorrelationError(
                f"unresolved recovery release voi={voi}: missing reservation reference",
            )
        actions.append(ReservationReleased(
            cid=claim.cid, venue_offer_id=voi, size_usdt=claim.size_usdt,
            reason="missing_from_venue", signal_correlation_id=claim.signal_correlation_id,
            account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            symbol=claim.symbol,
            reservation_ref=claim.reservation_ref,
        ))

    # stale PENDING (crash-mid-flight, outcome unknown) -> UNKNOWN.  Absence
    # from the active snapshot is not evidence of rejection; the next full
    # observation/history pass must resolve the uncertainty.
    for c in local_claims:
        if c.state == RegistryState.PENDING and (now_ms - c.occurred_at_ms) >= grace_ms:
            if c.symbol not in configured_symbols:
                raise ValueError(
                    f"recovery fail for cid={c.cid} has symbol={c.symbol!r} "
                    f"not in configured {sorted(configured_symbols)}")
            if c.reservation_ref is None:
                raise RecoveryCorrelationError(
                    f"unresolved recovery pending cid={c.cid}: missing reservation reference",
                )
            actions.append(ReservationUnknown(
                cid=c.cid, size_usdt=c.size_usdt,
                signal_correlation_id=c.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated,
                reason="unresolved_at_boot", occurred_at_ms=now_ms,
                symbol=c.symbol,
                reservation_ref=c.reservation_ref,
            ))

    return actions


def _is_transient_status(status_code: int) -> bool:
    """Transient = retryable: transport/network (0), rate-limit (429), 5xx.
    4xx (auth/bad-request) is deterministic — never retried."""
    return status_code == 0 or status_code == 429 or status_code >= 500


class _ActiveOffersQuery(Protocol):
    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> list[ActiveFundingOffer]: ...


class _ActiveCreditsQuery(Protocol):
    async def get_active_funding_credits(
        self, *, ctx: AccountContext, symbol: str | None = None,
    ) -> list[ActiveFundingCredit]: ...


class _WalletsQuery(Protocol):
    async def get_funding_available(
        self, *, ctx: AccountContext, currency: str,
    ) -> Decimal: ...

    async def get_funding_available_all(
        self, *, ctx: AccountContext,
    ) -> Mapping[str, Decimal]: ...


class _OfferHistoryQuery(Protocol):
    async def get_funding_offer_history(
        self,
        *,
        ctx: AccountContext,
        start_ms: int,
        end_ms: int,
        symbol: str | None = None,
    ) -> FundingOfferHistory: ...


class _AuthRestQuery(
    _ActiveOffersQuery,
    _ActiveCreditsQuery,
    _WalletsQuery,
    _OfferHistoryQuery,
    Protocol,
):
    """Combined protocol: offers + credits + wallet-available queries."""


class _Bus(Protocol):
    async def publish(self, event: Any) -> None: ...


class _FsmSink(Protocol):
    # Distinct from _Bus on purpose: recovery FSM events are delivered DIRECTLY to
    # the OfferRegistry (which satisfies .handle), bypassing the bus so reconcile-time
    # exposure stays single-writer (only PositionReconciled reaches the ledger's bus).
    async def handle(self, event: Any) -> None: ...


class BootRecovery:
    """Boot orchestration: full-account venue reconcile + crash recovery.

    Runs once at the start of Daemon.run(), live only. Fetches all venue offers,
    credits and funding-wallet balances (with retry -> fail-safe), then persists
    recovery events plus one immutable observation in ONE txn (no REST call held
    inside the txn). After commit, publishes derived PositionReconciled signals
    and routes only correlated CLAIMED/RELEASED lifecycle events to the registry.
    UNKNOWN and orphan quarantine are durable states, never automatic retries.
    """

    def __init__(
        self,
        *,
        store: PostgresEventStore,
        session_factory: async_sessionmaker[AsyncSession],
        auth_rest: _AuthRestQuery,
        account_ctx: AccountContext,
        deployment_environment: str,
        bus: _Bus,
        offer_registry: _FsmSink | None = None,
        is_simulated: bool = False,
        symbol: str | None = None,
        symbols: list[str] | None = None,
        grace_ms: int = 120_000,
        action_grace_ms: int = 0,
        max_attempts: int = 3,
        backoff_base_s: float = 1.0,
        clock: Callable[[], int] | None = None,
        uncertainty_handler: Callable[[ReservationUnknown], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._auth_rest = auth_rest
        self._ctx = account_ctx
        self._env = deployment_environment
        self._bus = bus
        self._offer_registry = offer_registry
        self._is_simulated = is_simulated
        # Configured symbols drive the per-symbol reconcile loop. Back-compat:
        # the legacy single `symbol` kwarg maps to a 1-element list. Dedup while
        # preserving order so a misconfigured duplicate cell can't fire twice.
        if symbols is not None:
            raw = symbols
        elif symbol is not None:
            raw = [symbol]
        else:
            raise ValueError("BootRecovery requires `symbols` (preferred) or the legacy `symbol`")
        seen: set[str] = set()
        self._symbols: list[str] = []
        for s in raw:
            if s not in seen:
                seen.add(s)
                self._symbols.append(s)
        self._grace_ms = grace_ms
        self._action_grace_ms = action_grace_ms
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._uncertainty_handler = uncertainty_handler

    async def run(self) -> ReconcileResult:
        # A reconcile is one account observation.  The venue calls intentionally
        # happen before opening the database transaction, so the event writer
        # sees a coherent immutable result and never holds a lock over network IO.
        history_start_ms = await self._load_history_start_ms()
        query_started_at_ms = self._clock()
        all_offers = await self._fetch_offers(None)
        all_credits = await self._fetch_credits(None)
        wallet_available = await self._fetch_available_all()
        history = await self._fetch_history(
            start_ms=history_start_ms,
            end_ms=query_started_at_ms,
        )
        query_finished_at_ms = self._clock()
        per_symbol = self._group_snapshot(
            offers=all_offers,
            credits=all_credits,
            wallet_available=wallet_available,
        )
        snapshot_event = VenueSnapshotObserved(
            account_id=self._ctx.account_id,
            environment=self._env,
            query_started_at_ms=query_started_at_ms,
            query_finished_at_ms=query_finished_at_ms,
            offers=tuple(self._offer_observation(o) for o in all_offers),
            credits=tuple(self._credit_observation(c) for c in all_credits),
            offer_history=tuple(self._offer_observation(o) for o in history.offers),
            wallet_available=wallet_available,
            coverage=SnapshotCoverage(
                active_offers_complete=True,
                active_credits_complete=True,
                wallets_complete=True,
                active_offer_pages=1,
                active_credit_pages=1,
                wallet_pages=1,
                offer_history_complete=history.coverage.complete,
                offer_history_pages=history.coverage.pages,
                offer_history_start_ms=history.coverage.requested_start_ms,
                offer_history_end_ms=history.coverage.requested_end_ms,
                offer_history_oldest_mts=history.coverage.oldest_mts_created,
                offer_history_newest_mts=history.coverage.newest_mts_created,
            ),
            occurred_at_ms=query_finished_at_ms,
        )

        async with session_scope(self._session_factory) as session:
            local_claims = await self._load_local_claims(session)
            actions = compute_recovery_actions(
                venue_offers=all_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=query_finished_at_ms, grace_ms=self._grace_ms,
                action_grace_ms=self._action_grace_ms,
                configured_symbols=frozenset(self._symbols),
            )
            unknown_actions = [ev for ev in actions if isinstance(ev, ReservationUnknown)]
            for unknown_action in unknown_actions:
                await self._store.append(session, unknown_action)
            snapshot_drift = await self._append_snapshot_event(session, snapshot_event)

            matched_events: list[SubmitMatchedToVenueOffer] = []
            matched_offer_ids: set[str] = set()
            unknown_attempts = (
                await self._load_unknown_attempts(session)
                if snapshot_drift.event_seq is not None
                else []
            )
            possible_active_match_ids = {
                offer.venue_offer_id
                for attempt in unknown_attempts
                for offer in all_offers
                if _offer_matches_attempt_identity(
                    attempt,
                    offer,
                    observed_end_ms=query_started_at_ms,
                )
            }
            for attempt in unknown_attempts:
                match = match_unknown_attempt(
                    attempt,
                    all_offers,
                    history.offers,
                    history.coverage,
                )
                if match.kind != "exact_match" or match.offer is None:
                    continue
                assert snapshot_drift.event_seq is not None
                offer = match.offer
                matched_offer_ids.add(offer.venue_offer_id)
                matched = SubmitMatchedToVenueOffer(
                    symbol=attempt.symbol,
                    cid=attempt.cid,
                    venue_offer_id=offer.venue_offer_id,
                    signal_correlation_id=attempt.signal_correlation_id,
                    account_id=attempt.account_id,
                    is_simulated=self._is_simulated,
                    venue_status=offer.status,
                    matched_mts_created=offer.mts_created,
                    reconcile_event_seq=snapshot_drift.event_seq,
                    amount=attempt.amount,
                    reservation_ref=attempt.reservation_ref.bind_venue_offer(
                        offer.venue_offer_id
                    ),
                    occurred_at_ms=query_finished_at_ms,
                )
                await self._store.append(session, matched)
                matched_events.append(matched)

            remaining_actions = [
                ev
                for ev in actions
                if not isinstance(ev, ReservationUnknown)
                and not (
                    isinstance(ev, VenueOfferQuarantined)
                    and ev.venue_offer_id
                    in (matched_offer_ids | possible_active_match_ids)
                )
            ]
            persisted_remaining_actions: list[RecoveryAction] = []
            for remaining_action in remaining_actions:
                append_result = await self._store.append(session, remaining_action)
                was_persisted = (
                    append_result
                    if isinstance(append_result, bool)
                    else getattr(append_result, "persisted", True)
                )
                if was_persisted:
                    persisted_remaining_actions.append(remaining_action)

        persisted_actions: list[RecoveryAction | SubmitMatchedToVenueOffer] = [
            *unknown_actions,
            *matched_events,
            *persisted_remaining_actions,
        ]

        # The API may resolve/bind a claim in another process.  Refresh at the
        # committed reconcile boundary before routing later WS lifecycle events
        # so an in-memory registry can never remain permanently unaware of the
        # durable venue identity.
        refresh_registry = (
            getattr(self._offer_registry, "refresh_from_snapshot", None)
            if self._offer_registry is not None
            else None
        )
        if refresh_registry is not None:
            async with self._session_factory() as refresh_session:
                await refresh_registry(
                    refresh_session,
                    account_id=self._ctx.account_id,
                    deployment_environment=self._env,
                )

        # Publish derived per-symbol signals only after the immutable observation
        # and all recovery events have committed.  Unknown venue symbols are
        # included, while deployment remains scoped to configured symbols.
        agg_reserved = Decimal("0")
        agg_realized = Decimal("0")
        agg_available = Decimal("0")
        agg_n_credits = 0
        for snap in per_symbol:
            await self._safe_publish(PositionReconciled(
                account_id=self._ctx.account_id,
                symbol=snap.symbol,
                reserved=snap.reserved,
                realized=snap.realized,
                available=snap.available,
                n_offers=len(snap.offers),
                n_credits=len(snap.credits),
                occurred_at_ms=query_finished_at_ms,
            ))
            agg_reserved += snap.reserved
            agg_realized += snap.realized
            agg_available += snap.available
            agg_n_credits += len(snap.credits)

        n_claim = n_release = n_fail = n_unknown = n_quarantined = n_matched = 0
        for persisted_action in persisted_actions:
            if isinstance(persisted_action, ReservationClaimed):
                n_claim += 1
                await self._route_fsm(persisted_action)
            elif isinstance(persisted_action, ReservationReleased):
                n_release += 1
                await self._route_fsm(persisted_action)
            elif isinstance(persisted_action, ReservationFailed):
                n_fail += 1
            elif isinstance(persisted_action, ReservationUnknown):
                n_unknown += 1
                if self._uncertainty_handler is None and not self._is_simulated:
                    # A live recovery that cannot update the local command gate
                    # must fail closed; returning normally would let the next
                    # deployment tick submit against an unresolved PENDING.
                    raise RuntimeError(
                        "live recovery requires an uncertainty_handler"
                    )
                if self._uncertainty_handler is not None:
                    await self._uncertainty_handler(persisted_action)
            elif isinstance(persisted_action, VenueOfferQuarantined):
                n_quarantined += 1
            elif isinstance(persisted_action, SubmitMatchedToVenueOffer):
                n_matched += 1
        log.info(
            "reconcile_complete symbols=%d venue_offers=%d "
            "reserved=%.2f realized=%.2f available=%.2f "
            "claims=%d released=%d failed=%d unknown=%d quarantined=%d matched=%d",
            len(per_symbol), len(all_offers),
            float(agg_reserved), float(agg_realized), float(agg_available),
            n_claim, n_release, n_fail, n_unknown, n_quarantined, n_matched,
        )
        return ReconcileResult(
            n_claimed=n_claim, n_released=n_release, n_failed=n_fail,
            reserved_usdt=agg_reserved, realized_usdt=agg_realized,
            available_usdt=agg_available,
            n_credits=agg_n_credits,
            reserved_drift_usdt=snapshot_drift.reserved_drift,
            realized_drift_usdt=snapshot_drift.realized_drift,
            venue_offers=tuple(all_offers),
            n_unknown=n_unknown,
            n_quarantined=n_quarantined,
            n_matched=n_matched,
            snapshot_event_seq=snapshot_drift.event_seq,
        )

    def _group_snapshot(
        self,
        *,
        offers: list[ActiveFundingOffer],
        credits: list[ActiveFundingCredit],
        wallet_available: Mapping[str, Decimal],
    ) -> list[_SymbolSnapshot]:
        symbols = (
            set(self._symbols)
            | {offer.symbol for offer in offers}
            | {credit.symbol for credit in credits}
            | set(wallet_available)
        )
        return [
            _SymbolSnapshot(
                symbol=symbol,
                offers=[offer for offer in offers if offer.symbol == symbol],
                credits=[credit for credit in credits if credit.symbol == symbol],
                available=wallet_available.get(symbol, Decimal("0")),
                reserved=sum(
                    (offer.amount for offer in offers if offer.symbol == symbol),
                    Decimal("0"),
                ),
                realized=sum(
                    (credit.amount for credit in credits if credit.symbol == symbol),
                    Decimal("0"),
                ),
            )
            for symbol in sorted(symbols)
        ]

    @staticmethod
    def _offer_observation(offer: ActiveFundingOffer) -> VenueOfferObservation:
        return VenueOfferObservation(
            venue_offer_id=offer.venue_offer_id,
            symbol=offer.symbol,
            amount_original=offer.amount_original or offer.amount,
            amount_remaining=offer.amount,
            rate=(
                offer.rate_decimal
                if offer.rate_decimal is not None
                else Decimal(str(offer.rate)) if offer.rate is not None else None
            ),
            period_days=offer.period_days,
            status=offer.status,
            mts_created=offer.mts_created,
            mts_updated=offer.mts_updated or offer.mts_created,
            offer_type=offer.offer_type,
            flags=_normalize_flags(offer.flags),
        )

    @staticmethod
    def _credit_observation(credit: ActiveFundingCredit) -> VenueCreditObservation:
        return VenueCreditObservation(
            credit_id=credit.credit_id,
            symbol=credit.symbol,
            amount=credit.amount,
            rate=Decimal(str(credit.rate)) if credit.rate is not None else None,
            period_days=credit.period_days,
            status=credit.status,
            mts_created=credit.mts_created,
            mts_updated=credit.mts_updated,
            flags=_normalize_flags(credit.flags),
        )

    async def _load_history_start_ms(self) -> int | None:
        """Read the oldest unresolved durable attempt before any venue call."""
        try:
            canonical = UUID(self._ctx.account_id)
        except ValueError:
            return None
        async with self._session_factory() as session:
            values = (
                await session.execute(
                    select(SubmissionAttemptRow.started_at_ms)
                    .join(
                        ExecutionUncertaintyRow,
                        ExecutionUncertaintyRow.attempt_id == SubmissionAttemptRow.attempt_id,
                    )
                    .where(
                        SubmissionAttemptRow.exchange_account_id == canonical,
                        SubmissionAttemptRow.deployment_environment == self._env,
                        SubmissionAttemptRow.outcome_kind == SubmitOutcomeKind.UNKNOWN.value,
                        ExecutionUncertaintyRow.exchange_account_id == canonical,
                        ExecutionUncertaintyRow.deployment_environment == self._env,
                        ExecutionUncertaintyRow.symbol == SubmissionAttemptRow.symbol,
                        ExecutionUncertaintyRow.kind == "submit_outcome_unknown",
                        ExecutionUncertaintyRow.state == "open",
                    )
                )
            ).scalars().all()
        return min(values) if values else None

    async def _fetch_history(
        self,
        *,
        start_ms: int | None,
        end_ms: int,
    ) -> FundingOfferHistory:
        """History is supplemental evidence: failures remain incomplete/UNKNOWN."""
        if start_ms is None:
            return FundingOfferHistory(
                offers=(),
                coverage=FundingOfferHistoryCoverage(
                    requested_start_ms=end_ms,
                    requested_end_ms=end_ms,
                    oldest_mts_created=None,
                    newest_mts_created=None,
                    pages=0,
                    complete=False,
                ),
            )
        query = getattr(self._auth_rest, "get_funding_offer_history", None)
        if query is None:
            return FundingOfferHistory(
                offers=(),
                coverage=FundingOfferHistoryCoverage(
                    requested_start_ms=start_ms,
                    requested_end_ms=end_ms,
                    oldest_mts_created=None,
                    newest_mts_created=None,
                    pages=0,
                    complete=False,
                ),
            )
        try:
            result = await query(
                ctx=self._ctx,
                start_ms=start_ms,
                end_ms=end_ms,
                symbol=None,
            )
            raw_history = cast(FundingOfferHistory, result)
            offers = tuple(raw_history.offers)
            raw_coverage = raw_history.coverage
            coverage = FundingOfferHistoryCoverage(
                requested_start_ms=raw_coverage.requested_start_ms,
                requested_end_ms=raw_coverage.requested_end_ms,
                oldest_mts_created=raw_coverage.oldest_mts_created,
                newest_mts_created=raw_coverage.newest_mts_created,
                pages=raw_coverage.pages,
                complete=raw_coverage.complete,
            )
            if any(not is_terminal_offer_status(offer.status) for offer in offers):
                log.warning("offer_history_evidence_incomplete non_terminal_status")
                return FundingOfferHistory(
                    offers=(),
                    coverage=FundingOfferHistoryCoverage(
                        requested_start_ms=coverage.requested_start_ms,
                        requested_end_ms=coverage.requested_end_ms,
                        oldest_mts_created=coverage.oldest_mts_created,
                        newest_mts_created=coverage.newest_mts_created,
                        pages=coverage.pages,
                        complete=False,
                    ),
                )
            return FundingOfferHistory(offers=offers, coverage=coverage)
        except (
            ArithmeticError,
            AttributeError,
            BitfinexAPIError,
            BitfinexShapeError,
            TypeError,
            ValueError,
        ) as exc:
            log.warning("offer_history_evidence_incomplete err=%r", exc)
            return FundingOfferHistory(
                offers=(),
                coverage=FundingOfferHistoryCoverage(
                    requested_start_ms=start_ms,
                    requested_end_ms=end_ms,
                    oldest_mts_created=None,
                    newest_mts_created=None,
                    pages=0,
                    complete=False,
                ),
            )

    async def _load_unknown_attempts(
        self,
        session: AsyncSession,
    ) -> list[UnknownSubmitAttempt]:
        """Load only typed UNKNOWN attempts with complete audited submit shape."""
        try:
            canonical = UUID(self._ctx.account_id)
        except ValueError:
            return []
        attempts = (
            await session.execute(
                select(SubmissionAttemptRow)
                .join(
                    ExecutionUncertaintyRow,
                    ExecutionUncertaintyRow.attempt_id == SubmissionAttemptRow.attempt_id,
                )
                .where(
                    SubmissionAttemptRow.exchange_account_id == canonical,
                    SubmissionAttemptRow.deployment_environment == self._env,
                    SubmissionAttemptRow.outcome_kind == SubmitOutcomeKind.UNKNOWN.value,
                    ExecutionUncertaintyRow.exchange_account_id == canonical,
                    ExecutionUncertaintyRow.deployment_environment == self._env,
                    ExecutionUncertaintyRow.symbol == SubmissionAttemptRow.symbol,
                    ExecutionUncertaintyRow.kind == "submit_outcome_unknown",
                    ExecutionUncertaintyRow.state == "open",
                )
            )
        ).scalars().all()
        claims = (
            await session.execute(
                select(OfferClaimRow).where(
                    OfferClaimRow.exchange_account_id == canonical,
                    OfferClaimRow.deployment_environment == self._env,
                    OfferClaimRow.state == RegistryState.UNKNOWN.value,
                )
            )
        ).scalars().all()
        claims_by_decision = {
            claim.execution_decision_id: claim
            for claim in claims
            if claim.execution_decision_id is not None
        }
        result: list[UnknownSubmitAttempt] = []
        for attempt in attempts:
            claim = claims_by_decision.get(attempt.execution_decision_id)
            if claim is None:
                continue
            try:
                signal_id = UUID(claim.signal_correlation_id)
            except (TypeError, ValueError):
                continue
            parsed = attempt_from_row(attempt, signal_correlation_id=signal_id)
            if parsed is None:
                continue
            if parsed.amount != Decimal(str(claim.size_usdt)):
                continue
            result.append(parsed)
        return result

    async def _append_snapshot_event(
        self,
        session: AsyncSession,
        event: VenueSnapshotObserved,
    ) -> Any:
        """Append the immutable observation through the canonical store API."""
        return await self._store.append_snapshot(session, event)

    async def _fetch_offers(self, symbol: str | None) -> list[ActiveFundingOffer]:
        """Fetch venue offers with bounded retry on TRANSIENT failures only.
        4xx re-raises immediately; transient exhaustion re-raises too. Either way
        the daemon fails to start (fail-safe: never trade without venue truth)."""
        last_exc: BitfinexAPIError | None = None
        for attempt in range(self._max_attempts):
            try:
                return await self._auth_rest.get_active_funding_offers(
                    ctx=self._ctx, symbol=symbol,
                )
            except BitfinexAPIError as e:
                if not _is_transient_status(e.status_code):
                    log.error(
                        "boot_recovery_venue_fetch_fatal status=%d err=%r — failing startup",
                        e.status_code, e,
                    )
                    raise
                last_exc = e
                if attempt + 1 < self._max_attempts:
                    backoff = self._backoff_base_s * (2 ** attempt)
                    log.warning(
                        "boot_recovery_venue_fetch_transient attempt=%d/%d status=%d backoff=%.1fs",
                        attempt + 1, self._max_attempts, e.status_code, backoff,
                    )
                    await asyncio.sleep(backoff)
        log.error("boot_recovery_venue_unreachable after %d attempts — failing startup", self._max_attempts)
        assert last_exc is not None
        raise last_exc

    async def _fetch_credits(self, symbol: str | None) -> list[ActiveFundingCredit]:
        """Fetch venue credits with bounded retry on TRANSIENT failures only.
        4xx re-raises immediately; transient exhaustion re-raises too.
        Fail-fast: never trade without knowing realized exposure."""
        last_exc: BitfinexAPIError | None = None
        for attempt in range(self._max_attempts):
            try:
                return await self._auth_rest.get_active_funding_credits(
                    ctx=self._ctx, symbol=symbol,
                )
            except BitfinexAPIError as e:
                if not _is_transient_status(e.status_code):
                    log.error(
                        "boot_recovery_credits_fetch_fatal status=%d err=%r — failing startup",
                        e.status_code, e,
                    )
                    raise
                last_exc = e
                if attempt + 1 < self._max_attempts:
                    backoff = self._backoff_base_s * (2 ** attempt)
                    log.warning(
                        "boot_recovery_credits_fetch_transient attempt=%d/%d status=%d backoff=%.1fs",
                        attempt + 1, self._max_attempts, e.status_code, backoff,
                    )
                    await asyncio.sleep(backoff)
        log.error("boot_recovery_credits_unreachable after %d attempts — failing startup", self._max_attempts)
        assert last_exc is not None
        raise last_exc

    async def _fetch_available_all(self) -> Mapping[str, Decimal]:
        """Fetch every funding-wallet balance with bounded transient retry."""
        query_all = getattr(self._auth_rest, "get_funding_available_all", None)
        if query_all is None:
            # Compatibility for older adapters: this is only a fallback, while
            # the production BitfinexAuthREST implementation always exposes the
            # account-wide endpoint.
            values: dict[str, Decimal] = {}
            for symbol in self._symbols:
                values[symbol] = await self._fetch_available(symbol)
            return values

        last_exc: BitfinexAPIError | None = None
        for attempt in range(self._max_attempts):
            try:
                result = await query_all(ctx=self._ctx)
                return {
                    str(symbol): Decimal(str(amount))
                    for symbol, amount in result.items()
                }
            except BitfinexAPIError as e:
                if not _is_transient_status(e.status_code):
                    log.error(
                        "boot_recovery_wallets_fetch_fatal status=%d — failing reconcile",
                        e.status_code,
                    )
                    raise
                last_exc = e
                if attempt + 1 < self._max_attempts:
                    backoff = self._backoff_base_s * (2 ** attempt)
                    log.warning(
                        "boot_recovery_wallets_fetch_transient attempt=%d/%d status=%d backoff=%.1fs",
                        attempt + 1, self._max_attempts, e.status_code, backoff,
                    )
                    await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    async def _fetch_available(self, symbol: str) -> Decimal:
        """Fetch funding-wallet available balance with bounded retry on TRANSIENT
        failures only. 4xx re-raises immediately; transient exhaustion re-raises.
        Same fail-safe contract as offers/credits: a persistent failure aborts the
        reconcile tick, so deploy() is skipped (never size against unknown funds).
        Currency = symbol minus the leading 'f' (fUST -> UST)."""
        currency = symbol[1:] if symbol.startswith("f") else symbol
        last_exc: BitfinexAPIError | None = None
        for attempt in range(self._max_attempts):
            try:
                return await self._auth_rest.get_funding_available(
                    ctx=self._ctx, currency=currency,
                )
            except BitfinexAPIError as e:
                if not _is_transient_status(e.status_code):
                    log.error(
                        "boot_recovery_wallets_fetch_fatal status=%d err=%r — failing reconcile",
                        e.status_code, e,
                    )
                    raise
                last_exc = e
                if attempt + 1 < self._max_attempts:
                    backoff = self._backoff_base_s * (2 ** attempt)
                    log.warning(
                        "boot_recovery_wallets_fetch_transient attempt=%d/%d status=%d backoff=%.1fs",
                        attempt + 1, self._max_attempts, e.status_code, backoff,
                    )
                    await asyncio.sleep(backoff)
        log.error("boot_recovery_wallets_unreachable after %d attempts — failing reconcile", self._max_attempts)
        assert last_exc is not None
        raise last_exc

    async def _load_local_claims(self, session: AsyncSession) -> list[LocalClaim]:
        rows = (await session.execute(
            select(OfferClaimRow).where(
                account_scope_clause(
                    session,
                    account_id=self._ctx.account_id,
                    exchange_account_column=OfferClaimRow.exchange_account_id,
                    legacy_account_column=OfferClaimRow.account_id,
                ),
                OfferClaimRow.deployment_environment == self._env,
            )
        )).scalars().all()
        return [
            LocalClaim(
                cid=r.cid, venue_offer_id=r.venue_offer_id,
                state=RegistryState(r.state), size_usdt=Decimal(str(r.size_usdt)),
                signal_correlation_id=UUID(r.signal_correlation_id),
                occurred_at_ms=r.occurred_at_ms, symbol=r.symbol,
                reservation_ref=(
                    ReservationRef(
                        execution_decision_id=r.execution_decision_id,
                        cid=r.cid,
                        signal_correlation_id=UUID(r.signal_correlation_id),
                        venue_offer_id=r.venue_offer_id,
                    )
                    if r.execution_decision_id is not None
                    else None
                ),
            )
            for r in rows
        ]

    async def _safe_publish(self, event: object) -> None:
        try:
            await self._bus.publish(event)
        except Exception as exc:
            log.critical(
                "boot_recovery_publish_failed event=%s err=%r — projection lost, SoT persisted",
                type(event).__name__, exc,
            )

    async def _route_fsm(self, event: object) -> None:
        """Recovery FSM events go to the registry directly (NOT the ledger's bus),
        so reconcile-time exposure stays single-writer (PositionReconciled).
        Falls back to the bus when no registry is wired."""
        if self._offer_registry is not None:
            try:
                await self._offer_registry.handle(event)
            except Exception as exc:
                log.critical(
                    "boot_recovery_fsm_route_failed event=%s err=%r — FSM projection lost, SoT persisted",
                    type(event).__name__, exc,
                )
        else:
            await self._safe_publish(event)
