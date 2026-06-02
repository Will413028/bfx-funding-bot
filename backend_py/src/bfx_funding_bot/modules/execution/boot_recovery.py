"""Boot-time venue reconciliation (Phase 4.4c / 3a-recovery).

The venue (Bitfinex) is the ultimate truth for which funding offers exist;
the PG event_log + snapshot is the SoT for our intent + accounting. Bitfinex
funding offers carry NO client cid (submit drops it, response lacks it), so we
reconcile by venue_offer_id -- the only stable shared key.

  - orphan  (venue has voi, local has no CLAIMED row)  -> ReservationClaimed
  - missing (local CLAIMED, venue no longer has voi)   -> ReservationReleased
  - stale PENDING (write-ahead intent, unresolvable)   -> ReservationFailed

PENDING can't be matched to the venue (no cid round-trip), so it converges to
FAILED after reconcile -- capital-neutral, because the actual offer (if the
submit reached the venue) is captured independently by orphan-claim.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NamedTuple, Protocol
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.external.bitfinex.auth_rest import ActiveFundingCredit, ActiveFundingOffer
from bfx_funding_bot.external.bitfinex.cid import BITFINEX_CID_MAX
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.execution.events import (
    PositionReconciled,
    ReservationClaimed,
    ReservationFailed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.registry_offers import RegistryState

log = logging.getLogger(__name__)

RecoveryAction = ReservationClaimed | ReservationReleased | ReservationFailed


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


class _SymbolSnapshot(NamedTuple):
    symbol: str
    offers: list[ActiveFundingOffer]
    credits: list[ActiveFundingCredit]
    available: Decimal
    reserved: Decimal
    realized: Decimal


# Fixed namespace for deterministic synthetic correlation ids on reconciled
# orphans (offers with no originating local signal).
_RECOVERY_SCID_NS = UUID("3a000000-0000-4000-8000-000000000001")


@dataclass(frozen=True, slots=True)
class LocalClaim:
    cid: int
    venue_offer_id: str | None
    state: RegistryState
    size_usdt: Decimal
    signal_correlation_id: UUID
    occurred_at_ms: int


def synth_orphan_cid(venue_offer_id: str) -> int:
    """Synthetic cid for a reconciled orphan claim, in the NEGATIVE namespace.

    Real cids (generate_cid) are blake2b masked by BITFINEX_CID_MAX -> always
    positive, so negating guarantees zero collision with a client-submitted
    intent. Synthetic cids are never sent to the venue (orphans are reconciled,
    not submitted); a negative cid in event_log marks "no originating intent".
    Numeric voi (the normal case) is negated directly so re-running reconcile
    upserts the same offer_claims row; non-numeric voi falls back to a hashed
    value, also negated.
    """
    try:
        return -int(venue_offer_id)
    except ValueError:
        digest = hashlib.blake2b(venue_offer_id.encode(), digest_size=8).digest()
        return -(int.from_bytes(digest, "big") & BITFINEX_CID_MAX)


def synth_orphan_scid(venue_offer_id: str) -> UUID:
    """Deterministic correlation id for a reconciled orphan (no local signal)."""
    return uuid5(_RECOVERY_SCID_NS, venue_offer_id)


def compute_recovery_actions(
    *,
    venue_offers: list[ActiveFundingOffer],
    local_claims: list[LocalClaim],
    account_id: str,
    is_simulated: bool,
    now_ms: int,
    grace_ms: int,
    action_grace_ms: int = 0,
    symbol: str,
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

    # orphan: venue has it, local CLAIMED set doesn't -> claim (reserved += size)
    for voi, offer in venue_by_voi.items():
        if voi in claimed_by_voi:
            continue
        if (now_ms - offer.mts_created) < action_grace_ms:
            continue  # too fresh — local claim may still be committing
        actions.append(ReservationClaimed(
            cid=synth_orphan_cid(voi), venue_offer_id=voi,
            size_usdt=offer.amount, signal_correlation_id=synth_orphan_scid(voi),
            account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            symbol=offer.symbol,
        ))

    # missing: local CLAIMED, venue gone -> release (reserved -= size)
    for voi, claim in claimed_by_voi.items():
        if voi in venue_by_voi:
            continue
        if (now_ms - claim.occurred_at_ms) < action_grace_ms:
            continue  # too fresh — venue snapshot may lag the just-placed offer
        actions.append(ReservationReleased(
            cid=claim.cid, venue_offer_id=voi, size_usdt=claim.size_usdt,
            reason="missing_from_venue", signal_correlation_id=claim.signal_correlation_id,
            account_id=account_id, is_simulated=is_simulated, occurred_at_ms=now_ms,
            symbol=symbol,
        ))

    # stale PENDING (crash-mid-flight, unmatchable) -> FAILED (capital-neutral)
    for c in local_claims:
        if c.state == RegistryState.PENDING and (now_ms - c.occurred_at_ms) >= grace_ms:
            actions.append(ReservationFailed(
                cid=c.cid, size_usdt=c.size_usdt,
                signal_correlation_id=c.signal_correlation_id,
                account_id=account_id, is_simulated=is_simulated,
                reason="unresolved_at_boot", occurred_at_ms=now_ms,
                symbol=symbol,
            ))

    return actions


def _is_transient_status(status_code: int) -> bool:
    """Transient = retryable: transport/network (0), rate-limit (429), 5xx.
    4xx (auth/bad-request) is deterministic — never retried."""
    return status_code == 0 or status_code == 429 or status_code >= 500


class _ActiveOffersQuery(Protocol):
    async def get_active_funding_offers(
        self, *, ctx: AccountContext, symbol: str,
    ) -> list[ActiveFundingOffer]: ...


class _ActiveCreditsQuery(Protocol):
    async def get_active_funding_credits(
        self, *, ctx: AccountContext, symbol: str,
    ) -> list[ActiveFundingCredit]: ...


class _WalletsQuery(Protocol):
    async def get_funding_available(
        self, *, ctx: AccountContext, currency: str,
    ) -> Decimal: ...


class _AuthRestQuery(_ActiveOffersQuery, _ActiveCreditsQuery, _WalletsQuery, Protocol):
    """Combined protocol: offers + credits + wallet-available queries."""


class _Bus(Protocol):
    async def publish(self, event: Any) -> None: ...


class _FsmSink(Protocol):
    # Distinct from _Bus on purpose: recovery FSM events are delivered DIRECTLY to
    # the OfferRegistry (which satisfies .handle), bypassing the bus so reconcile-time
    # exposure stays single-writer (only PositionReconciled reaches the ledger's bus).
    async def handle(self, event: Any) -> None: ...


class BootRecovery:
    """Boot orchestration: venue reconcile + resolve crash-mid-flight PENDING.

    Runs once at the start of Daemon.run(), live only. Fetches venue offers
    (with retry -> fail-safe), then persists corrections in ONE txn (no REST
    call held inside the txn). After commit, publishes CLAIMED/RELEASED to the
    bus for in-memory projections (FAILED is not published -- no subscriber,
    reserved untouched). Idempotent across boots: terminal states are excluded
    from the next boot's diff.
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

    async def run(self) -> ReconcileResult:
        # Per-symbol reconcile: each configured currency is an independent wallet
        # (native units), so offers/credits/available are queried per symbol and a
        # PositionReconciled is published per symbol. The FSM recovery diff
        # (orphan-claim / missing-release of offer_claims) stays GLOBAL: offer_claims
        # carries no symbol and venue_offer_id is globally unique on Bitfinex, so we
        # union venue offers across symbols before diffing local claims — otherwise a
        # claim for symbol B would look "missing_from_venue" while reconciling symbol A
        # and be spuriously released. ReconcileResult aggregates across symbols (the
        # PeriodicReconcile divergence/drift logic is per-tick, not per-symbol).
        now_ms = self._clock()
        per_symbol: list[_SymbolSnapshot] = []
        all_offers: list[ActiveFundingOffer] = []
        for symbol in self._symbols:
            offers = await self._fetch_offers(symbol)
            credits = await self._fetch_credits(symbol)
            available = await self._fetch_available(symbol)
            per_symbol.append(_SymbolSnapshot(
                symbol=symbol,
                offers=offers,
                credits=credits,
                available=available,
                reserved=sum((o.amount for o in offers), Decimal("0")),
                realized=sum((c.amount for c in credits), Decimal("0")),
            ))
            all_offers.extend(offers)

        agg_reserved = Decimal("0")
        agg_realized = Decimal("0")
        agg_available = Decimal("0")
        agg_n_credits = 0
        agg_reserved_drift = Decimal("0")
        agg_realized_drift = Decimal("0")

        async with session_scope(self._session_factory) as session:
            local_claims = await self._load_local_claims(session)
            # GLOBAL FSM diff against the union of all symbols' venue offers.
            # missing-claim releases stamp the primary symbol; LocalClaim has no
            # per-claim symbol yet (Phase 2: offer_claims.symbol).
            actions = compute_recovery_actions(
                venue_offers=all_offers, local_claims=local_claims,
                account_id=self._ctx.account_id, is_simulated=self._is_simulated,
                now_ms=now_ms, grace_ms=self._grace_ms,
                action_grace_ms=self._action_grace_ms,
                symbol=self._symbols[0],
            )
            for ev in actions:
                await self._store.append(session, ev)
            # Per-symbol absolute position snapshot (single-writer per symbol).
            for snap in per_symbol:
                drift = await self._store.set_position_snapshot(
                    session,
                    account_id=self._ctx.account_id,
                    symbol=snap.symbol,
                    reserved_usdt=snap.reserved,
                    realized_usdt=snap.realized,
                    n_offers=len(snap.offers),
                    n_credits=len(snap.credits),
                    occurred_at_ms=now_ms,
                )
                agg_reserved_drift += drift.reserved_drift
                agg_realized_drift += drift.realized_drift

        # Publish one PositionReconciled per symbol AFTER durable commit.
        for snap in per_symbol:
            await self._safe_publish(PositionReconciled(
                account_id=self._ctx.account_id,
                symbol=snap.symbol,
                reserved=snap.reserved,
                realized=snap.realized,
                available=snap.available,
                n_offers=len(snap.offers),
                n_credits=len(snap.credits),
                occurred_at_ms=now_ms,
            ))
            agg_reserved += snap.reserved
            agg_realized += snap.realized
            agg_available += snap.available
            agg_n_credits += len(snap.credits)

        n_claim = n_release = n_fail = 0
        for ev in actions:
            if isinstance(ev, ReservationClaimed):
                n_claim += 1
                await self._route_fsm(ev)
            elif isinstance(ev, ReservationReleased):
                n_release += 1
                await self._route_fsm(ev)
            elif isinstance(ev, ReservationFailed):
                n_fail += 1
        log.info(
            "reconcile_complete symbols=%d venue_offers=%d "
            "reserved=%.2f realized=%.2f available=%.2f "
            "orphans_claimed=%d released=%d pending_failed=%d",
            len(self._symbols), len(all_offers),
            float(agg_reserved), float(agg_realized), float(agg_available),
            n_claim, n_release, n_fail,
        )
        return ReconcileResult(
            n_claimed=n_claim, n_released=n_release, n_failed=n_fail,
            reserved_usdt=agg_reserved, realized_usdt=agg_realized,
            available_usdt=agg_available,
            n_credits=agg_n_credits,
            reserved_drift_usdt=agg_reserved_drift,
            realized_drift_usdt=agg_realized_drift,
        )

    async def _fetch_offers(self, symbol: str) -> list[ActiveFundingOffer]:
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

    async def _fetch_credits(self, symbol: str) -> list[ActiveFundingCredit]:
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
                OfferClaimRow.account_id == self._ctx.account_id,
                OfferClaimRow.deployment_environment == self._env,
            )
        )).scalars().all()
        return [
            LocalClaim(
                cid=r.cid, venue_offer_id=r.venue_offer_id,
                state=RegistryState(r.state), size_usdt=Decimal(str(r.size_usdt)),
                signal_correlation_id=UUID(r.signal_correlation_id),
                occurred_at_ms=r.occurred_at_ms,
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
