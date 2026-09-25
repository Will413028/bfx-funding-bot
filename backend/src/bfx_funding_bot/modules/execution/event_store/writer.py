"""Transaction-owned account event append and projection cursor updates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_canonical,
    account_id_uuid_or_none,
    account_scope_clause,
)
from bfx_funding_bot.modules.accounts.tables import ExchangeAccount
from bfx_funding_bot.modules.execution.event_store.serialization import deserialize_stored_event
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, ProjectionHeadRow

if TYPE_CHECKING:
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore

PROJECTION_NAME = "execution_state"
DEFAULT_PROJECTOR_VERSION = "execution-state-v1"
_READY_PROJECTOR_MIGRATIONS = frozenset({
    "c2e3f4a5b6c7",
    "cd5e6f708192",
    "de6f708192a3",
    "e7b1c2d3e4f5",
    "f8c2d4e6a901",
    "a9d3e5f7b102",
    # Each of these adds storage beside the ledger and leaves projection_heads,
    # the seeded cursor and the projector contract untouched: a prefix-hash side
    # table, then two capital_snapshots columns.
    "c3f5a1d7e204",
    "d1b7c2e4a305",
    "e5c9a3f10b62",
    "b4e6f8a0c203",
    # Adds trading_halt.kind and relaxes one release_sessions transition guard.
    # Touches neither projection_heads, the seeded cursor, nor the projector.
    "a7f3c1d9e204",
    # Adds the append-only trading_state table beside the ledger; no event,
    # projection or cursor is touched.
    "8e4f33517b10",
    # Adds the append-only funding_cancel_all_audit table; no event, projection
    # or cursor is touched.
    "c2b7b04da604",
    "c3a639388457",
    # Approvals, operator requests and trading_state.probation_floor; no event,
    # projection or cursor is touched.
    "0218f9ab59a2",
    # Adds the operator adjudication outbox beside the ledger and revokes web API
    # writes; the rows, the cursor and the projector contract are unchanged.
    "b8e2d4f6a013",
})

__all__ = [
    "DEFAULT_PROJECTOR_VERSION",
    "PROJECTION_NAME",
    "AccountEventWriter",
    "AppendResult",
    "ProjectionWriteError",
]


class ProjectionWriteError(RuntimeError):
    """The event row could not be projected atomically with its append."""


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Durability, ordering, and cursor outcome for one event append."""

    event_seq: int
    persisted: bool
    projection_head: int | None
    projector_version: str

    @property
    def deduplicated(self) -> bool:
        return not self.persisted


class AccountEventWriter:
    """Serialize one account stream and atomically advance its projection head.

    The writer deliberately does not commit.  The caller owns the surrounding
    transaction, so a projection error can roll back the event row, all
    snapshots, and the cursor together.
    """

    def __init__(
        self,
        *,
        store: PostgresEventStore,
        projector_version: str = DEFAULT_PROJECTOR_VERSION,
        strict_identity: bool = True,
        allow_missing_account: bool = False,
    ) -> None:
        self._store = store
        self._projector_version = projector_version
        self._strict_identity = strict_identity
        self._allow_missing_account = allow_missing_account

    async def acquire_lock(
        self,
        session: AsyncSession,
        *,
        account_id: UUID | str,
    ) -> int | None:
        """Acquire this writer's account/environment xact lock once per batch."""
        canonical = account_id_uuid_or_none(account_id)
        if canonical is None:
            if self._strict_identity or await self._database_has_contract_marker(session):
                raise ValueError(
                    "serialized event writer requires a canonical ExchangeAccount UUID"
                )
            return None
        return await acquire_transaction_lock(
            session,
            account_id=account_id_canonical(canonical),
            deployment_environment=self._store.deployment_environment,
        )

    async def append(self, session: AsyncSession, event: object) -> AppendResult:
        """Append/project one event while holding its account transaction lock."""
        await self._assert_projector_migration_ready(session)
        account_id = self._event_account_id(event)
        await self.acquire_lock(session, account_id=account_id)
        return await self._append_locked(session, event, account_id=account_id)

    async def prepare_locked(self, session: AsyncSession, *, account_id: UUID) -> None:
        """Acquire the xact lock, validate readiness and replay before a guard read.

        Caller owns commit/rollback and must use this same session for guard,
        decision and intent writes. No venue I/O belongs in this transaction.
        """
        await self._assert_projector_migration_ready(session)
        await self.acquire_lock(session, account_id=account_id)
        await self._ensure_account_writable(session, account_id=account_id)
        await self._replay_pending(session, account_id=account_id)

    async def append_batch(
        self, session: AsyncSession, events: list[object] | tuple[object, ...]
    ) -> list[AppendResult]:
        """Append a domain-time-ordered batch under one account lock.

        Sorting is deliberately limited to this explicit batch API.  A single
        event is always persisted at the database's next ``event_seq``; ties
        retain caller order so no random UUID is used as a hidden clock.
        """
        if not events:
            return []
        await self._assert_projector_migration_ready(session)
        account_ids = [self._event_account_id(event) for event in events]
        canonical_ids = [account_id_uuid_or_none(account_id) for account_id in account_ids]
        stream_ids = [
            str(canonical) if canonical is not None else str(account_id)
            for account_id, canonical in zip(account_ids, canonical_ids, strict=True)
        ]
        if any(stream_id != stream_ids[0] for stream_id in stream_ids[1:]):
            raise ValueError("append_batch requires one account/environment stream")
        ordered = sorted(
            zip(events, account_ids, strict=True),
            key=lambda item: getattr(item[0], "occurred_at_ms", None) or 0,
        )
        await self.acquire_lock(session, account_id=ordered[0][1])
        return [
            await self._append_locked(session, event, account_id=account_id)
            for event, account_id in ordered
        ]

    async def _append_locked(
        self,
        session: AsyncSession,
        event: object,
        *,
        account_id: UUID | str,
    ) -> AppendResult:
        try:
            await self._ensure_account_writable(session, account_id=account_id)
            await self._replay_pending(
                session,
                account_id=account_id,
            )
            outcome = await self._store._append_unlocked(session, event)
            projection_head = await self._advance_projection_head(
                session,
                account_id=account_id,
                event_seq=outcome.event_seq,
            )
        except ProjectionWriteError:
            raise
        except Exception as exc:
            raise ProjectionWriteError(
                f"serialized projection failed for account={account_id}: {exc}"
            ) from exc
        return AppendResult(
            event_seq=outcome.event_seq,
            persisted=outcome.persisted,
            projection_head=projection_head,
            projector_version=self._projector_version,
        )

    async def _ensure_account_writable(
        self,
        session: AsyncSession,
        *,
        account_id: UUID | str,
    ) -> None:
        """Fail before INSERT when the migrated account registry is incomplete."""
        canonical = account_id_uuid_or_none(account_id)
        contract_marker = await self._database_has_contract_marker(session)
        if canonical is None:
            if self._strict_identity or contract_marker:
                raise ValueError(
                    "serialized event writer requires a canonical ExchangeAccount UUID"
                )
            return
        account_exists = await session.scalar(
            select(ExchangeAccount.id).where(ExchangeAccount.id == canonical)
        )
        if account_exists is None and (
            not self._allow_missing_account or contract_marker
        ):
            raise ValueError(f"ExchangeAccount does not exist: {canonical}")

    async def _replay_pending(
        self,
        session: AsyncSession,
        *,
        account_id: UUID | str,
    ) -> None:
        """Replay durable rows after the account stream's projection cursor.

        The transaction advisory lock makes the cursor read and every replayed
        snapshot update one serialized critical section.  A missing cursor is
        treated as genesis; the cutover migration seeds cursors for historical
        snapshots before this writer is enabled.
        """
        canonical = account_id_uuid_or_none(account_id)
        if canonical is None:
            return
        if self._allow_missing_account:
            account_exists = await session.scalar(
                select(ExchangeAccount.id).where(ExchangeAccount.id == canonical)
            )
            if account_exists is None:
                # Legacy synthetic PostgreSQL fixtures have no account registry
                # and no cursor. Their snapshots were maintained synchronously
                # by the compatibility append path, so replaying from cursor
                # zero would double-apply every historical delta.
                return
        cursor = await self._projection_cursor(session, canonical)
        rows = (
            await session.execute(
                select(EventLogRow)
                .where(
                    account_scope_clause(
                        session,
                        account_id=str(canonical),
                        exchange_account_column=EventLogRow.exchange_account_id,
                        legacy_account_column=EventLogRow.account_id,
                    ),
                    EventLogRow.deployment_environment
                    == self._store.deployment_environment,
                    EventLogRow.event_seq > cursor,
                )
                .order_by(EventLogRow.event_seq.asc())
            )
        ).scalars().all()
        for row in rows:
            replayed = deserialize_stored_event(row)
            await self._store._project_event_unlocked(
                session,
                replayed,
                account_id=str(canonical),
                event_seq=row.event_seq,
            )
            await self._advance_projection_head(
                session,
                account_id=canonical,
                event_seq=row.event_seq,
            )

    async def _projection_cursor(
        self,
        session: AsyncSession,
        account_id: UUID,
    ) -> int:
        stmt = select(ProjectionHeadRow.last_event_seq).where(
            ProjectionHeadRow.exchange_account_id == account_id,
            ProjectionHeadRow.deployment_environment
            == self._store.deployment_environment,
            ProjectionHeadRow.projection_name == PROJECTION_NAME,
        )
        cursor = await session.scalar(stmt)
        return int(cursor or 0)

    async def _advance_projection_head(
        self,
        session: AsyncSession,
        *,
        account_id: UUID | str,
        event_seq: int,
    ) -> int | None:
        canonical = account_id_uuid_or_none(account_id)
        if canonical is None:
            if self._strict_identity:
                raise ValueError(
                    "projection head requires a canonical ExchangeAccount UUID"
                )
            return None

        account_exists = await session.scalar(
            select(ExchangeAccount.id).where(ExchangeAccount.id == canonical)
        )
        if account_exists is None:
            if (
                not self._allow_missing_account
                or await self._database_has_contract_marker(session)
            ):
                raise ValueError(f"ExchangeAccount does not exist: {canonical}")
            # Only an unmigrated SQLite/legacy test schema may omit the account
            # registry.  A production Alembic marker makes this branch fail
            # closed, even when a compatibility adapter constructed this writer.
            return None

        stmt = select(ProjectionHeadRow).where(
            ProjectionHeadRow.exchange_account_id == canonical,
            ProjectionHeadRow.deployment_environment
            == self._store.deployment_environment,
            ProjectionHeadRow.projection_name == PROJECTION_NAME,
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        head = await session.scalar(stmt)
        if head is None:
            head = ProjectionHeadRow(
                exchange_account_id=canonical,
                deployment_environment=self._store.deployment_environment,
                projection_name=PROJECTION_NAME,
                last_event_seq=event_seq,
                projector_version=self._projector_version,
            )
            session.add(head)
        else:
            head.last_event_seq = max(head.last_event_seq, event_seq)
            head.projector_version = self._projector_version
            head.updated_at = cast(Any, func.current_timestamp())
        await session.flush()
        return int(head.last_event_seq)

    @staticmethod
    async def _database_migration_revisions(
        session: AsyncSession,
    ) -> tuple[str, ...]:
        """Read the Alembic revision set, or empty for direct-create fixtures."""
        bind = session.bind
        if bind is None or bind.dialect.name != "postgresql":
            return ()
        marker = await session.scalar(
            text("SELECT to_regclass('public.alembic_version')")
        )
        if marker is None:
            return ()
        revisions = (
            await session.execute(
                text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
            )
        ).scalars().all()
        return tuple(str(revision) for revision in revisions)

    async def _assert_projector_migration_ready(self, session: AsyncSession) -> None:
        """Fail closed until the historical cursor seed has been applied.

        ``bc4d5e6f7081`` created the cursor table but left it empty for existing
        streams.  Running the replay writer at that intermediate revision would
        double-apply legacy snapshots.  The explicit allow-list is intentionally
        updated alongside each later migration that preserves the seeded cursor
        contract.
        """
        revisions = await self._database_migration_revisions(session)
        if revisions and not set(revisions).issubset(_READY_PROJECTOR_MIGRATIONS):
            raise ValueError(
                "serialized projector cursor migration incomplete: "
                f"database revisions={revisions!r}, "
                f"requires one of {sorted(_READY_PROJECTOR_MIGRATIONS)!r}"
            )

    async def _database_has_contract_marker(self, session: AsyncSession) -> bool:
        """Return whether this session is on the Alembic-managed production DB.

        Historical unit/PG fixtures create tables directly with SQLAlchemy and
        intentionally have no ``alembic_version`` marker.  Live deployments
        always run Alembic, so the marker is the narrow boundary at which the
        compatibility adapter loses permission to accept legacy account IDs or
        missing registry rows.
        """
        return bool(await self._database_migration_revisions(session))

    @staticmethod
    def _event_account_id(event: object) -> UUID | str:
        account_id: Any = getattr(event, "account_id", None)
        if account_id is None:
            raise ValueError("serialized event requires account_id")
        return cast(UUID | str, account_id)
