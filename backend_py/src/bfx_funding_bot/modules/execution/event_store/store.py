from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_uuid_or_none,
    account_scope_clause,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_stored_event,
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
    ReconcileObservationRow,
)
from bfx_funding_bot.modules.execution.events import (
    __SCHEMA_VERSION__,
    DEFAULT_RECONCILE_SYMBOL,
)
from bfx_funding_bot.modules.execution.registry_offers import RegistryState


@dataclass(frozen=True, slots=True)
class SnapshotDrift:
    reserved_drift: Decimal
    realized_drift: Decimal


@dataclass(frozen=True, slots=True)
class StoreAppendResult:
    """Internal append outcome returned to the account writer."""

    persisted: bool
    event_seq: int


class OfferClaimIdentityConflictError(RuntimeError):
    """A CID projection attempted to change an established reservation identity."""


# Event types whose re-delivery must be deduped (idempotent fills/releases).
# NOTE: dedup for events with venue_seq IS NULL is app-level only — the
# uq_event_log_dedup unique index does not constrain NULLs (PG treats them as
# distinct). WS-sourced fills/releases carry venue_seq, so this gap is narrow.
# RESERVATION_CLAIMED is intentionally excluded: orphan-claim idempotency is
# enforced at the reconciliation compute layer (boot_recovery.compute_recovery_actions
# skips vois already in CLAIMED state), not by this store-level dedup.
_DEDUP_TYPES = frozenset({"ORDER_FILL", "RESERVATION_RELEASED"})

# Audit/attribution-only events: appended to event_log but NEVER projected onto
# position_state (live or rebuild tail) — realized/reserved stay reconcile-owned
# (single-writer invariant, ADR 2026-05-29).
_AUDIT_ONLY_TYPES = frozenset({"CREDIT_CLOSED"})

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
    """Append-only event log plus compatibility projection primitives.

    ``append`` is the public compatibility wrapper.  The serialized writer
    owns the transaction lock and projection cursor, then calls
    ``_append_unlocked`` for the actual event/legacy projection work.
    """

    def __init__(self, *, deployment_environment: str) -> None:
        self._env = deployment_environment

    @property
    def deployment_environment(self) -> str:
        """Environment dimension used by all projections in this store."""
        return self._env

    async def append(self, session: AsyncSession, event: object) -> bool:
        """Append through :class:`AccountEventWriter` without committing.

        ``strict_identity=False`` is a deliberately transitional compatibility
        path for the repository's legacy synthetic realms.  Production rows
        already have canonical UUID ownership after the Halt 1 migration; new
        callers should instantiate ``AccountEventWriter`` directly.
        """
        from bfx_funding_bot.modules.execution.event_store.writer import (
            AccountEventWriter,
        )

        writer = AccountEventWriter(
            store=self,
            strict_identity=False,
            allow_missing_account=True,
        )
        result = await writer.append(session, event)
        return result.persisted

    async def _append_unlocked(
        self, session: AsyncSession, event: object
    ) -> StoreAppendResult:
        """Append/project one event after the account lock is held."""
        etype = event_type_of(event)
        # Cast to Any so attribute access works on the dynamically-typed event object.
        _ev: Any = cast(Any, event)
        account_id: str = str(_ev.account_id)
        exchange_account_id = account_id_uuid_or_none(account_id)
        venue_offer_id: str | None = getattr(_ev, "venue_offer_id", None)
        venue_seq: int | None = getattr(_ev, "venue_seq", None)
        cid: int | None = getattr(_ev, "cid", None)
        occurred_at_ms: int = _ev.occurred_at_ms or 0
        payload: dict[str, Any] = serialize_event(event)

        existing_seq = None
        event_id = getattr(_ev, "event_id", None)
        if event_id is not None:
            existing = await self._find_event_id_row(
                session, account_id, event_id
            )
            if existing is not None:
                if existing.event_type != etype or existing.payload != payload:
                    raise OfferClaimIdentityConflictError(
                        f"event identity conflict event_id={event_id}"
                    )
                return StoreAppendResult(persisted=False, event_seq=existing.event_seq)
        if etype in _DEDUP_TYPES:
            existing_seq = await self._find_logged_seq(
                session, account_id, etype, venue_offer_id, venue_seq
            )
        if existing_seq is not None:
            return StoreAppendResult(persisted=False, event_seq=existing_seq)

        row = EventLogRow(
            account_id=account_id,
            exchange_account_id=exchange_account_id,
            deployment_environment=self._env,
            event_type=etype,
            cid=cid,
            venue_offer_id=venue_offer_id,
            venue_seq=venue_seq,
            event_id=getattr(_ev, "event_id", None),
            schema_version=getattr(_ev, "schema_version", __SCHEMA_VERSION__),
            payload=payload,
            occurred_at_ms=occurred_at_ms,
        )
        session.add(row)
        await session.flush()  # assigns row.event_seq
        await self._project_event_unlocked(
            session,
            event,
            account_id=account_id,
            event_seq=row.event_seq,
        )
        return StoreAppendResult(persisted=True, event_seq=row.event_seq)

    async def _project_event_unlocked(
        self,
        session: AsyncSession,
        event: object,
        *,
        account_id: str,
        event_seq: int,
    ) -> None:
        """Apply one already-persisted event to the compatibility snapshots.

        The account writer uses the same primitive for the current append and
        for replaying rows that were durable before a projection transaction
        completed.  Keeping this operation independent from event insertion is
        what makes the event-log cursor a recoverable projection boundary.
        """
        etype = event_type_of(event)
        if etype in _AUDIT_ONLY_TYPES:
            return
        event_obj: Any = cast(Any, event)
        projection_changed = await self._project_offer_claims(
            session, event, account_id, event_seq=event_seq
        )
        if projection_changed:
            await self._project_position_state(
                session,
                etype,
                account_id,
                getattr(event_obj, "amount", None),
                event_seq,
                event_obj.occurred_at_ms or 0,
                symbol=event_obj.symbol,
            )

    async def _find_event_id_row(
        self, session: AsyncSession, account_id: str, event_id: object
    ) -> EventLogRow | None:
        stmt = select(EventLogRow).where(
            account_scope_clause(
                session,
                account_id=account_id,
                exchange_account_column=EventLogRow.exchange_account_id,
                legacy_account_column=EventLogRow.account_id,
            ),
            EventLogRow.deployment_environment == self._env,
            EventLogRow.event_id == event_id,
        ).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _find_logged_seq(
        self, session: AsyncSession, account_id: str, event_type: str,
        venue_offer_id: str | None, venue_seq: int | None,
    ) -> int | None:
        stmt = (
            select(EventLogRow.event_seq)
            .where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=EventLogRow.exchange_account_id,
                    legacy_account_column=EventLogRow.account_id,
                ),
                EventLogRow.deployment_environment == self._env,
                EventLogRow.event_type == event_type,
                EventLogRow.venue_offer_id == venue_offer_id,
                EventLogRow.venue_seq == venue_seq,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _already_logged(
        self, session: AsyncSession, account_id: str, event_type: str,
        venue_offer_id: str | None, venue_seq: int | None,
    ) -> bool:
        return (
            await self._find_logged_seq(
                session, account_id, event_type, venue_offer_id, venue_seq
            )
        ) is not None

    async def _project_offer_claims(
        self,
        session: AsyncSession,
        event: object,
        account_id: str,
        *,
        event_seq: int | None = None,
    ) -> bool:
        """Project event onto the cid-keyed offer_claims snapshot (same txn).

        Direct event_type -> state mapping; no pre-select, no voi-keyed
        transition(). cid is stable across the whole lifecycle so a single
        row is upserted in place (PENDING -> CLAIMED -> RELEASED/FAILED).
        """
        etype = event_type_of(event)
        state = _CLAIM_STATE_BY_TYPE.get(etype)
        if state is None:
            return False  # audit-only events (e.g. cancel) are not claim-bearing
        _ev: Any = cast(Any, event)
        cid: int | None = getattr(_ev, "cid", None)
        if cid is None:
            return False  # no cid -> nothing to key on
        now_ms: int = _ev.occurred_at_ms or 0
        # All claim-bearing events now carry canonical native `amount` (mirrored
        # from size_usdt by events._resolve_amount); use it directly.
        symbol = getattr(_ev, "symbol", None)
        if symbol is None:
            raise ValueError(f"{etype} reached offer_claims projection without symbol")
        return await self._upsert_claim(
            session,
            cid=cid,
            account_id=account_id,
            state=state,
            venue_offer_id=getattr(_ev, "venue_offer_id", None),
            symbol=symbol,
            size_usdt=Decimal(str(_ev.amount)),
            signal_correlation_id=str(_ev.signal_correlation_id),
            execution_decision_id=(
                _ev.reservation_ref.execution_decision_id
                if getattr(_ev, "reservation_ref", None) is not None
                else None
            ),
            occurred_at_ms=now_ms,
            last_updated_ms=now_ms,
            last_event_seq=event_seq or 0,
        )

    async def _upsert_claim(
        self,
        session: AsyncSession,
        *,
        cid: int,
        account_id: str,
        state: RegistryState,
        venue_offer_id: str | None,
        symbol: str,
        size_usdt: Decimal,
        signal_correlation_id: str,
        execution_decision_id: str | None,
        occurred_at_ms: int,
        last_updated_ms: int,
        last_event_seq: int = 0,
    ) -> bool:
        """Atomically insert then compare every available reservation identity.

        The database owns concurrent first-writer arbitration.  A losing writer
        re-reads the canonical row by CID, venue offer id, or audited decision
        id and may only continue for an exact identity match.
        """
        values: dict[str, Any] = {
            "cid": cid,
            "account_id": account_id,
            "exchange_account_id": account_id_uuid_or_none(account_id),
            "deployment_environment": self._env,
            "symbol": symbol,
            "state": state.value,
            "venue_offer_id": venue_offer_id,
            "size_usdt": size_usdt,
            "signal_correlation_id": signal_correlation_id,
            "execution_decision_id": execution_decision_id,
            "occurred_at_ms": occurred_at_ms,
            "last_updated_ms": last_updated_ms,
            "last_event_seq": last_event_seq,
        }
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        insert_result: Any = await session.execute(
            insert(OfferClaimRow).values(values).on_conflict_do_nothing(),
        )
        inserted = insert_result.rowcount == 1

        identity_terms = [OfferClaimRow.cid == cid]
        if venue_offer_id is not None:
            identity_terms.append(OfferClaimRow.venue_offer_id == venue_offer_id)
        if execution_decision_id is not None:
            identity_terms.append(OfferClaimRow.execution_decision_id == execution_decision_id)
        matches = (await session.execute(
            select(OfferClaimRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=OfferClaimRow.exchange_account_id,
                    legacy_account_column=OfferClaimRow.account_id,
                ),
                OfferClaimRow.deployment_environment == self._env,
                or_(*identity_terms),
            ),
        )).scalars().all()
        if len(matches) != 1:
            raise OfferClaimIdentityConflictError(
                f"claim identity conflict cid={cid}: canonical matches={len(matches)}",
            )
        existing = matches[0]
        if existing.signal_correlation_id != signal_correlation_id:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: signal")
        if existing.cid != cid:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: cid")
        if (
            existing.execution_decision_id is not None
            and existing.execution_decision_id != execution_decision_id
        ):
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: decision")
        if (
            existing.venue_offer_id is not None
            and existing.venue_offer_id != venue_offer_id
        ):
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: venue offer")
        if existing.symbol != symbol:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: symbol")
        if Decimal(str(existing.size_usdt)) != size_usdt:
            raise OfferClaimIdentityConflictError(f"claim identity conflict cid={cid}: amount")

        changed = (
            inserted
            or existing.state != state.value
            or last_event_seq > existing.last_event_seq
        )
        next_execution_decision_id = existing.execution_decision_id
        if next_execution_decision_id is None and execution_decision_id is not None:
            next_execution_decision_id = execution_decision_id
            changed = True
        next_venue_offer_id = existing.venue_offer_id
        if next_venue_offer_id is None and venue_offer_id is not None:
            next_venue_offer_id = venue_offer_id
            changed = True
        if changed:
            if existing.exchange_account_id is None:
                # SQLite historical synthetic fixtures use a NULL UUID owner
                # while production rows are UUID-keyed.  Core UPDATE avoids
                # asking SQLAlchemy to flush a legacy object whose final ORM
                # primary key is intentionally incomplete.
                session.expunge(existing)
                await session.execute(
                    update(OfferClaimRow)
                    .where(
                        OfferClaimRow.account_id == account_id,
                        OfferClaimRow.deployment_environment == self._env,
                        OfferClaimRow.cid == cid,
                    )
                    .values(
                        state=state.value,
                        last_updated_ms=last_updated_ms,
                        execution_decision_id=next_execution_decision_id,
                        venue_offer_id=next_venue_offer_id,
                        last_event_seq=max(existing.last_event_seq, last_event_seq),
                    )
                )
            else:
                existing.execution_decision_id = next_execution_decision_id
                existing.venue_offer_id = next_venue_offer_id
                existing.state = state.value
                existing.last_updated_ms = last_updated_ms
                existing.last_event_seq = max(existing.last_event_seq, last_event_seq)
        return changed

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
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=PositionStateRow.exchange_account_id,
                        legacy_account_column=PositionStateRow.account_id,
                    ),
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
            )
        ).scalar_one_or_none()
        is_new = ps is None
        if is_new:
            ps = PositionStateRow(
                account_id=account_id,
                exchange_account_id=account_id_uuid_or_none(account_id),
                deployment_environment=self._env,
                symbol=symbol,
                reserved=Decimal("0"),
                realized=Decimal("0"),
                last_updated_ms=0,
                last_event_seq=0,
            )
            session.add(ps)
        assert ps is not None
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
        if not is_new and ps.exchange_account_id is None:
            await session.execute(
                update(PositionStateRow)
                .where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
                .values(
                    reserved=reserved,
                    realized=realized,
                    last_updated_ms=occurred_at_ms,
                    last_event_seq=event_seq,
                )
            )
        else:
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
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=EventLogRow.exchange_account_id,
                        legacy_account_column=EventLogRow.account_id,
                    ),
                    EventLogRow.deployment_environment == self._env,
                )
            )
        ).scalar_one()

        ps = (
            await session.execute(
                select(PositionStateRow).where(
                    account_scope_clause(
                        session,
                        account_id=account_id,
                        exchange_account_column=PositionStateRow.exchange_account_id,
                        legacy_account_column=PositionStateRow.account_id,
                    ),
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
            )
        ).scalar_one_or_none()
        prior_reserved = Decimal(str(ps.reserved)) if ps is not None else Decimal("0")
        prior_realized = Decimal(str(ps.realized)) if ps is not None else Decimal("0")
        is_new = ps is None
        if is_new:
            ps = PositionStateRow(
                account_id=account_id,
                exchange_account_id=account_id_uuid_or_none(account_id),
                deployment_environment=self._env,
                symbol=symbol,
                reserved=Decimal("0"),
                realized=Decimal("0"),
                last_updated_ms=0,
                last_event_seq=0,
            )
            session.add(ps)
        assert ps is not None
        if not is_new and ps.exchange_account_id is None:
            await session.execute(
                update(PositionStateRow)
                .where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == self._env,
                    PositionStateRow.symbol == symbol,
                )
                .values(
                    reserved=reserved_usdt,
                    realized=realized_usdt,
                    last_updated_ms=occurred_at_ms,
                    last_event_seq=fence,
                    last_reconciled_at=occurred_at_ms,
                    n_credits=n_credits,
                )
            )
        else:
            ps.reserved = reserved_usdt
            ps.realized = realized_usdt
            ps.last_updated_ms = occurred_at_ms
            ps.last_event_seq = fence
            ps.last_reconciled_at = occurred_at_ms
            ps.n_credits = n_credits

        session.add(ReconcileObservationRow(
            account_id=account_id,
            exchange_account_id=account_id_uuid_or_none(account_id),
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
            account_scope_clause(
                session,
                account_id=account_id,
                exchange_account_column=OfferClaimRow.exchange_account_id,
                legacy_account_column=OfferClaimRow.account_id,
            ),
            OfferClaimRow.deployment_environment == deployment_environment))
        await session.execute(delete(PositionStateRow).where(
            account_scope_clause(
                session,
                account_id=account_id,
                exchange_account_column=PositionStateRow.exchange_account_id,
                legacy_account_column=PositionStateRow.account_id,
            ),
            PositionStateRow.deployment_environment == deployment_environment,
            PositionStateRow.symbol == symbol))
        await session.flush()

        rows = (await session.execute(
            select(EventLogRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=EventLogRow.exchange_account_id,
                    legacy_account_column=EventLogRow.account_id,
                ),
                EventLogRow.deployment_environment == deployment_environment,
            ).order_by(EventLogRow.event_seq.asc())
        )).scalars().all()

        # offer_claims: full fold.
        for r in rows:
            event = deserialize_stored_event(r)
            await self._project_offer_claims(
                session, event, account_id, event_seq=r.event_seq
            )

        # position_state: checkpoint + tail. The checkpoint base is now per-symbol
        # (reconcile_observation.symbol) so fUST/fUSD rebuild from their own base.
        checkpoint = (await session.execute(
            select(ReconcileObservationRow).where(
                account_scope_clause(
                    session,
                    account_id=account_id,
                    exchange_account_column=ReconcileObservationRow.exchange_account_id,
                    legacy_account_column=ReconcileObservationRow.account_id,
                ),
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
            exchange_account_id=account_id_uuid_or_none(account_id),
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
            if r.event_type in _AUDIT_ONLY_TYPES:
                continue  # mirror the live-append skip: no ledger fold, no seq bump
            # Phase 2: fUSD/fUST coexist in one event_log, so the tail fold MUST
            # filter on payload["symbol"] == symbol — otherwise the other
            # currency's deltas mix into this symbol's position_state row.
            # Legacy rows predate the symbol column (no `symbol` key); they were
            # fUST-era, so default a missing symbol to DEFAULT_RECONCILE_SYMBOL —
            # otherwise this filter drops them and silently miscomputes realized
            # on a genesis rebuild (the deploy pre-flight foot-gun).
            if ((r.payload or {}).get("symbol") or DEFAULT_RECONCILE_SYMBOL) != symbol:
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
