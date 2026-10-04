"""Durable `VenueEventStore` on PostgreSQL (`sim_venue_event`, insert-only).

The store is the venue's only door to the database, so the guards live here and not in the
caller: `open` reads the authority epoch on the store's OWN connection to the target
database and refuses unless the latest epoch is `ledger`. Nothing the caller passes can
stand in for that read (finding 5 of the P1a design review; ADR 2026-10-03).

Seam for `database_realm` (decision E2, its own later PR): `_check_database_realm` is where
the store will read the database's stamped realm on the same connection and compare it with
the account's. Until that table exists it checks nothing beyond the table's CHECK.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from bfx_funding_bot.modules.simulated_venue.contracts import (
    REQUIRED_AUTHORITY_EPOCH,
    ConcurrentAppendError,
    RealmRefusedError,
    SimAccount,
    VenueStoreError,
)
from bfx_funding_bot.modules.simulated_venue.events import (
    VenueEvent,
    event_from_payload,
    event_to_payload,
)
from bfx_funding_bot.modules.simulated_venue.tables import SimVenueEventRow

_T = SimVenueEventRow
_UNIQUE_VIOLATION = "23505"


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def _scope(account: SimAccount) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (
        _T.exchange_account_id == account.exchange_account_id,
        _T.deployment_environment == account.deployment_environment,
    )


class SqlVenueEventStore:
    """Use `SqlVenueEventStore.open(engine)`; the engine belongs to the caller."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    async def open(cls, engine: AsyncEngine) -> SqlVenueEventStore:
        """Refuse a database whose latest authority epoch is not `ledger`."""
        store = cls(engine)
        epoch = await store.authority_epoch()
        if epoch != REQUIRED_AUTHORITY_EPOCH:
            raise RealmRefusedError(
                f"simulated venue store needs authority epoch {REQUIRED_AUTHORITY_EPOCH!r}, "
                f"the database says {epoch!r}"
            )
        async with engine.connect() as conn:
            if await conn.scalar(text("SELECT to_regclass('public.sim_venue_event')")) is None:
                raise RealmRefusedError("this database has no sim_venue_event table (not migrated)")
            await store._check_database_realm(conn)
        return store

    async def authority_epoch(self) -> str:
        """The raw latest authority epoch, read on a connection of this store's engine.

        Raw on purpose: `core.authority.read_authority` refuses `ledger` while this build
        still supports only `legacy`, and the venue must see the true value.
        """
        try:
            async with self._engine.connect() as conn:
                if await conn.scalar(text("SELECT to_regclass('public.capital_authority_epoch')")) is None:
                    raise RealmRefusedError("authority epoch table is missing in this database")
                latest = await conn.scalar(text(
                    "SELECT authority FROM capital_authority_epoch ORDER BY epoch_seq DESC LIMIT 1"))
        except DBAPIError as exc:
            raise VenueStoreError(f"authority epoch unreadable: {exc!r}") from exc
        if not isinstance(latest, str) or not latest:
            raise RealmRefusedError("authority epoch has no row in this database")
        return latest

    async def _check_database_realm(self, conn: AsyncConnection) -> None:
        """Seam for E2's `database_realm` row: compare the stamped realm with the account's.

        Not implemented here by decision (own PR). When it lands, read the row on `conn`
        and refuse unless it names a realm in `ALLOWED_REALMS`.
        """

    async def load(self, account: SimAccount) -> Sequence[VenueEvent]:
        query = (
            select(_T.seq, _T.event_type, _T.schema_version, _T.payload)
            .where(*_scope(account)).order_by(_T.seq)
        )
        try:
            async with self._engine.connect() as conn:
                rows = (await conn.execute(query)).all()
        except DBAPIError as exc:
            raise VenueStoreError(f"load failed: {exc!r}") from exc
        events: list[VenueEvent] = []
        for position, (seq, event_type, version, payload) in enumerate(rows, start=1):
            if seq != position:
                raise VenueStoreError(f"{account}: log has a gap before seq {seq}")
            if payload.get("event_type") != event_type or payload.get("schema_version") != version:
                raise VenueStoreError(f"{account}: seq {seq} columns disagree with its payload")
            events.append(event_from_payload(payload))
        return tuple(events)

    async def append(
        self, account: SimAccount, expected_seq: int, events: Sequence[VenueEvent],
    ) -> None:
        """Append iff exactly `expected_seq` events exist; a lost race is ConcurrentAppendError."""
        try:
            async with self._engine.begin() as conn:
                current = await conn.scalar(
                    select(func.coalesce(func.max(_T.seq), 0)).where(*_scope(account)))
                if current != expected_seq:
                    raise ConcurrentAppendError(
                        f"{account}: expected seq {expected_seq}, log has {current} events")
                if events:
                    await conn.execute(insert(_T), [
                        {
                            "exchange_account_id": account.exchange_account_id,
                            "deployment_environment": account.deployment_environment,
                            "seq": expected_seq + offset,
                            "event_type": type(event).event_type,
                            "schema_version": event.schema_version,
                            "payload": event_to_payload(event),
                        }
                        for offset, event in enumerate(events, start=1)
                    ])
        except ConcurrentAppendError:
            raise
        except IntegrityError as exc:
            if _sqlstate(exc) == _UNIQUE_VIOLATION:
                raise ConcurrentAppendError(
                    f"{account}: lost the append race at seq {expected_seq + 1}") from exc
            raise VenueStoreError(f"append rejected: {exc!r}") from exc
        except DBAPIError as exc:
            raise VenueStoreError(f"append failed: {exc!r}") from exc
