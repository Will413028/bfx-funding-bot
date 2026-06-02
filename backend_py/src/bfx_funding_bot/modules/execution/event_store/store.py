from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    ReconcileObservationRow,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState


@dataclass(frozen=True, slots=True)
class SnapshotDrift:
    reserved_drift: Decimal
    realized_drift: Decimal


# Event types whose re-delivery must be deduped (idempotent fills/releases).
# NOTE: dedup for events with venue_seq IS NULL is app-level only — the
# uq_event_log_dedup unique index does not constrain NULLs (PG treats them as
# distinct). WS-sourced fills/releases carry venue_seq, so this gap is narrow.
# RESERVATION_CLAIMED is intentionally excluded: orphan-claim idempotency is
# enforced at the reconciliation compute layer (boot_recovery.compute_recovery_actions
# skips vois already in CLAIMED state), not by this store-level dedup.
_DEDUP_TYPES = frozenset({"ORDER_FILL", "RESERVATION_RELEASED"})

# Phase 2 / Task 11: the position-bearing events now carry a MANDATORY `symbol`
# and the snapshot/projection methods require an explicit `symbol` param. This
# constant survives ONLY as the projection fallback for the two symbol-less
# claim-lifecycle events — ReservationIntent / ReservationFailed — which have no
# `symbol` (or `amount`) field yet (adding them is a separate, later, gated
# step; see the breadcrumb near `claim_size` in _project_offer_claims). It is
# used solely at the append() projection call site (getattr(..., "symbol", None)
# or DEFAULT_RECONCILE_SYMBOL); the four real position events return their own
# symbol there, so this default never applies to them.
DEFAULT_RECONCILE_SYMBOL = "fUST"

# offer_claims FSM state by event_type — cid-keyed projection. The voi-keyed
# transition() (registry_offers.py) is reserved for the in-memory OfferRegistry's
# fill-tracking; this snapshot is keyed by cid (stable across the whole lifecycle).
_CLAIM_STATE_BY_TYPE: dict[str, RegistryState] = {
    "RESERVATION_INTENT": RegistryState.PENDING,
    "RESERVATION_CLAIMED": RegistryState.CLAIMED,
    "RESERVATION_FAILED": RegistryState.FAILED,
    "ORDER_FILL": RegistryState.RELEASED,
    "RESERVATION_RELEASED": RegistryState.RELEASED,
}


class PostgresEventStore:
    """Append-only event log + (later tasks) transactional snapshot maintenance."""

    def __init__(self, *, deployment_environment: str) -> None:
        self._env = deployment_environment

    async def append(self, session: AsyncSession, event: object) -> bool:
        """Append one domain event in the caller's txn. Returns False if deduped (skipped).

        Caller commits (e.g. via session_scope). Does NOT commit here.
        """
        etype = event_type_of(event)
        # Cast to Any so attribute access works on the dynamically-typed event object.
        _ev: Any = cast(Any, event)
        account_id: str = _ev.account_id
        venue_offer_id: str | None = getattr(_ev, "venue_offer_id", None)
        venue_seq: int | None = getattr(_ev, "venue_seq", None)
        cid: int | None = getattr(_ev, "cid", None)
        occurred_at_ms: int = _ev.occurred_at_ms or 0

        if etype in _DEDUP_TYPES and await self._already_logged(
            session, account_id, etype, venue_offer_id, venue_seq
        ):
            return False

        payload: dict[str, Any] = serialize_event(event)
        row = EventLogRow(
            account_id=account_id,
            deployment_environment=self._env,
            event_type=etype,
            cid=cid,
            venue_offer_id=venue_offer_id,
            venue_seq=venue_seq,
            payload=payload,
            occurred_at_ms=occurred_at_ms,
        )
        session.add(row)
        await session.flush()  # assigns row.event_seq
        # Snapshot maintenance (same txn).
        await self._project_offer_claims(session, event, account_id)
        await self._project_position_state(
            session, etype, account_id, getattr(_ev, "amount", None),
            row.event_seq, occurred_at_ms,
            symbol=getattr(_ev, "symbol", None) or DEFAULT_RECONCILE_SYMBOL,
        )
        return True

    async def _already_logged(
        self, session: AsyncSession, account_id: str, event_type: str,
        venue_offer_id: str | None, venue_seq: int | None,
    ) -> bool:
        stmt = (
            select(EventLogRow.event_seq)
            .where(
                EventLogRow.account_id == account_id,
                EventLogRow.deployment_environment == self._env,
                EventLogRow.event_type == event_type,
                EventLogRow.venue_offer_id == venue_offer_id,
                EventLogRow.venue_seq == venue_seq,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).first() is not None

    async def _project_offer_claims(
        self,
        session: AsyncSession,
        event: object,
        account_id: str,
    ) -> None:
        """Project event onto the cid-keyed offer_claims snapshot (same txn).

        Direct event_type -> state mapping; no pre-select, no voi-keyed
        transition(). cid is stable across the whole lifecycle so a single
        row is upserted in place (PENDING -> CLAIMED -> RELEASED/FAILED).
        """
        etype = event_type_of(event)
        state = _CLAIM_STATE_BY_TYPE.get(etype)
        if state is None:
            return  # audit-only events (e.g. cancel) are not claim-bearing
        _ev: Any = cast(Any, event)
        cid: int | None = getattr(_ev, "cid", None)
        if cid is None:
            return  # no cid -> nothing to key on
        now_ms: int = _ev.occurred_at_ms or 0
        # size_usdt= targets the offer_claims DB column (not renamed); the VALUE
        # source switches to the canonical event field `amount` where it exists.
        # ReservationIntent/ReservationFailed predate the rename and carry only
        # `size_usdt` (no `amount` field), so fall back to it. For the Claimed-
        # family events amount == size_usdt (see execution.events._resolve_amount),
        # so this is value-equivalent. `is not None` (not `or`) — a legitimate
        # Decimal("0") amount must NOT fall through to size_usdt on the money path.
        # NOTE(Task 11 symbol-mandatory): ReservationIntent/ReservationFailed also
        # lack a `symbol` field; making symbol mandatory must add amount+symbol to
        # those two events FIRST, after which this size_usdt fallback can be dropped.
        _amount = getattr(_ev, "amount", None)
        claim_size = _amount if _amount is not None else _ev.size_usdt
        await self._upsert_claim(
            session,
            cid=cid,
            account_id=account_id,
            state=state,
            venue_offer_id=getattr(_ev, "venue_offer_id", None),
            size_usdt=Decimal(str(claim_size)),
            signal_correlation_id=str(_ev.signal_correlation_id),
            occurred_at_ms=now_ms,
            last_updated_ms=now_ms,
        )

    async def _upsert_claim(
        self,
        session: AsyncSession,
        *,
        cid: int,
        account_id: str,
        state: RegistryState,
        venue_offer_id: str | None,
        size_usdt: Decimal,
        signal_correlation_id: str,
        occurred_at_ms: int,
        last_updated_ms: int,
    ) -> None:
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        values: dict[str, Any] = {
            "cid": cid,
            "account_id": account_id,
            "deployment_environment": self._env,
            "state": state.value,
            "venue_offer_id": venue_offer_id,
            "size_usdt": size_usdt,
            "signal_correlation_id": signal_correlation_id,
            "occurred_at_ms": occurred_at_ms,
            "last_updated_ms": last_updated_ms,
            # FSM state is the SoT for claims; position_state carries the
            # high-water mark, so last_event_seq stays 0 here (by-design,
            # carry-forward (d)).
            "last_event_seq": 0,
        }
        stmt = ins(OfferClaimRow).values(values).on_conflict_do_update(
            index_elements=["account_id", "deployment_environment", "cid"],
            set_={k: values[k] for k in ("state", "venue_offer_id", "last_updated_ms")},
        )
        await session.execute(stmt)
        # Core-level upsert bypasses the ORM, so any instance already loaded into
        # this session's identity map for the same composite PK is now stale.
        # Expire it so a subsequent select() re-fetches the updated row. The PK
        # tuple order follows the OfferClaimRow PrimaryKeyConstraint declaration.
        cached = session.identity_map.get(
            (OfferClaimRow, (account_id, self._env, cid), None)
        )
        if cached is not None:
            session.expire(cached)

    async def _project_position_state(
        self,
        session: AsyncSession,
        etype: str,
        account_id: str,
        size_usdt: Any,
        event_seq: int,
        occurred_at_ms: int,
        *,
        symbol: str,
    ) -> None:
        size = Decimal(str(size_usdt)) if size_usdt is not None else Decimal("0")
        ps = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
            )
        ).scalar_one_or_none()
        if ps is None:
            ps = PositionStateRow(
                account_id=account_id,
                deployment_environment=self._env,
                symbol=symbol,
                reserved=Decimal("0"),
                realized=Decimal("0"),
                last_updated_ms=0,
                last_event_seq=0,
            )
            session.add(ps)
        reserved = Decimal(str(ps.reserved))
        realized = Decimal(str(ps.realized))
        if etype == "RESERVATION_CLAIMED":
            reserved += size
        elif etype == "ORDER_FILL":
            delta = min(reserved, size)
            reserved -= delta
            realized += size
        elif etype == "RESERVATION_RELEASED":
            reserved -= min(reserved, size)
        ps.reserved = reserved
        ps.realized = realized
        ps.last_updated_ms = occurred_at_ms
        ps.last_event_seq = event_seq

    async def set_position_snapshot(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        reserved_usdt: Decimal,
        realized_usdt: Decimal,
        n_offers: int,
        n_credits: int,
        occurred_at_ms: int,
        symbol: str,
    ) -> SnapshotDrift:
        """Absolute venue snapshot for one symbol. Overwrites that symbol's live
        position_state view, appends an immutable reconcile_observation checkpoint
        (with the event_log fence), and returns drift vs the prior materialized
        belief.

        NOT a delta. NOT through the accumulator. Single-writer for exposure at
        reconcile time.

        n_offers is persisted in the checkpoint row only (audit); position_state
        carries n_credits but has no n_offers column. The reserved_usdt/realized_usdt
        PARAMS are native units of `symbol` (the name is legacy; never cross-symbol).
        """
        fence: int = (
            await session.execute(
                select(func.coalesce(func.max(EventLogRow.event_seq), 0)).where(
                    EventLogRow.account_id == account_id,
                    EventLogRow.deployment_environment == self._env,
                )
            )
        ).scalar_one()

        ps = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
            )
        ).scalar_one_or_none()
        prior_reserved = Decimal(str(ps.reserved)) if ps is not None else Decimal("0")
        prior_realized = Decimal(str(ps.realized)) if ps is not None else Decimal("0")
        if ps is None:
            ps = PositionStateRow(
                account_id=account_id,
                deployment_environment=self._env,
                symbol=symbol,
                reserved=Decimal("0"),
                realized=Decimal("0"),
                last_updated_ms=0,
                last_event_seq=0,
            )
            session.add(ps)
        ps.reserved = reserved_usdt
        ps.realized = realized_usdt
        ps.last_updated_ms = occurred_at_ms
        ps.last_event_seq = fence
        ps.last_reconciled_at = occurred_at_ms
        ps.n_credits = n_credits

        session.add(ReconcileObservationRow(
            account_id=account_id,
            deployment_environment=self._env,
            symbol=symbol,
            reserved_usdt=reserved_usdt,
            realized_usdt=realized_usdt,
            n_offers=n_offers,
            n_credits=n_credits,
            observed_at_ms=occurred_at_ms,
            event_seq_fence=fence,
        ))

        return SnapshotDrift(
            reserved_drift=abs(reserved_usdt - prior_reserved),
            realized_drift=abs(realized_usdt - prior_realized),
        )

    async def rebuild_snapshot_from_log(
        self, session: AsyncSession, *, account_id: str, deployment_environment: str,
        symbol: str,
    ) -> None:
        """Rebuild snapshots for (account, env).

        offer_claims: folded from the full event_log (FSM, cheap).
        position_state: latest reconcile_observation checkpoint ⊕ domain events
        with event_seq > fence. Falls back to genesis fold if no checkpoint.
        """
        await session.execute(delete(OfferClaimRow).where(
            OfferClaimRow.account_id == account_id,
            OfferClaimRow.deployment_environment == deployment_environment))
        await session.execute(delete(PositionStateRow).where(
            PositionStateRow.account_id == account_id,
            PositionStateRow.deployment_environment == deployment_environment,
            PositionStateRow.symbol == symbol))
        await session.flush()

        rows = (await session.execute(
            select(EventLogRow).where(
                EventLogRow.account_id == account_id,
                EventLogRow.deployment_environment == deployment_environment,
            ).order_by(EventLogRow.event_seq.asc())
        )).scalars().all()

        # offer_claims: full fold.
        for r in rows:
            event = deserialize_event(r.event_type, r.payload)
            await self._project_offer_claims(session, event, account_id)

        # position_state: checkpoint + tail. The checkpoint base is now per-symbol
        # (reconcile_observation.symbol) so fUST/fUSD rebuild from their own base.
        checkpoint = (await session.execute(
            select(ReconcileObservationRow).where(
                ReconcileObservationRow.account_id == account_id,
                ReconcileObservationRow.deployment_environment == deployment_environment,
                ReconcileObservationRow.symbol == symbol,
            ).order_by(ReconcileObservationRow.id.desc()).limit(1)
        )).scalar_one_or_none()

        if checkpoint is None:
            fence = 0
            base_reserved = Decimal("0")
            base_realized = Decimal("0")
            base_seq = 0
            base_ms = 0
        else:
            fence = checkpoint.event_seq_fence
            base_reserved = Decimal(str(checkpoint.reserved_usdt))
            base_realized = Decimal(str(checkpoint.realized_usdt))
            base_seq = checkpoint.event_seq_fence
            base_ms = checkpoint.observed_at_ms

        ps = PositionStateRow(
            account_id=account_id,
            deployment_environment=deployment_environment,
            symbol=symbol,
            reserved=base_reserved,
            realized=base_realized,
            last_updated_ms=base_ms,
            last_event_seq=base_seq,
            last_reconciled_at=(checkpoint.observed_at_ms if checkpoint else None),
            n_credits=(checkpoint.n_credits if checkpoint else None),
        )
        session.add(ps)

        reserved = base_reserved
        realized = base_realized
        for r in rows:
            if r.event_seq <= fence:
                continue
            # Phase 2: fUSD/fUST coexist in one event_log, so the tail fold MUST
            # filter on payload["symbol"] == symbol — otherwise the other
            # currency's deltas mix into this symbol's position_state row.
            if (r.payload or {}).get("symbol") != symbol:
                continue
            # Raw payload read (no deserialize_event) — intentional: the tail fold
            # only needs the native `amount` and stays decoupled from domain event
            # objects. If a new event type gains a non-string-serialized amount,
            # sync this with serialization.py.
            size = Decimal(str((r.payload or {}).get("amount", 0) or 0))
            if r.event_type == "RESERVATION_CLAIMED":
                reserved += size
            elif r.event_type == "ORDER_FILL":
                delta = min(reserved, size)
                reserved -= delta
                realized += size
            elif r.event_type == "RESERVATION_RELEASED":
                reserved -= min(reserved, size)
            ps.reserved = reserved
            ps.realized = realized
            ps.last_updated_ms = r.occurred_at_ms
            ps.last_event_seq = r.event_seq
