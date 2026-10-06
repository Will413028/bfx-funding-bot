"""Read-only boot check of a restored ledger database: this image's own restore-test entry.

``python -m bfx_funding_bot.apps.restore_boot_check`` with only the ephemeral read-only
DATABASE_URL of the restored cluster (restore_drill.py --restore-test runs it on the verifier
container, on the isolated network): it never sees production, R2, a venue or any application
secret, and it contacts no venue. It uses this image's own boot guards, so it judges the
restored copy the way a bot of this image would at boot, in one REPEATABLE READ READ ONLY
transaction:

1. schema at the image's single migration head (``assert_schema_head``);
2. the stamped realm, and every ledger scope in it;
3. the capital authority is ``ledger`` (``read_authority``);
4. the seed guard a Bitfinex bot boots through (``require_ledger_seed``) for every scope;
5. the ledger capital reader (``read_capital``) for every (symbol, cell) of each scope's newest
   accepted basis, as of that scope's newest query: it must fold a basis, or say the newest
   query is still pending (the restore point fell inside an observation cycle).

Prints one JSON line {"boot": {...}}; exit 3 with {"error": <bounded code>} on a refusal. The
drill's ``deploy/vm/pgbackrest/ledger_boot_check.py`` delegates here when the image has it, so
the checks always run against the API of the image they judge.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Final

from bfx_funding_bot.core.venue import Venue

EXIT_REFUSED = 3
VENUE: Final[Venue] = "bitfinex"
_PENDING = "snapshot_query_pending"


class RefusedError(RuntimeError):
    pass


async def _scopes(session: Any) -> list[Any]:
    from sqlalchemy import select, union

    from bfx_funding_bot.modules.ledger import Scope
    from bfx_funding_bot.modules.ledger.tables import (
        CapitalCommandClockRow,
        LedgerObservationQueryRow,
        QuarantineOpeningRow,
        SubmissionAttemptJournalRow,
    )

    owners = (CapitalCommandClockRow, LedgerObservationQueryRow, SubmissionAttemptJournalRow,
              QuarantineOpeningRow)
    statement = union(*(select(row.exchange_account_id, row.deployment_environment)
                        for row in owners))
    rows = (await session.execute(statement)).all()
    return sorted((Scope(row[0], row[1]) for row in rows),
                  key=lambda scope: (str(scope.exchange_account_id), scope.deployment_environment))


async def _reads(session: Any, scope: Any) -> tuple[str, list[dict[str, object]]]:
    from sqlalchemy import select

    from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS
    from bfx_funding_bot.modules.ledger.tables import (
        AcceptedCapitalBasisCellRow,
        AcceptedCapitalBasisRow,
        LedgerObservationQueryRow,
        LedgerObservationRow,
    )
    from bfx_funding_bot.modules.ledger.wiring import build_ledger_capital_reader
    from bfx_funding_bot.modules.trading.capital import Available, CapitalScope

    in_scope = (
        LedgerObservationQueryRow.exchange_account_id == scope.exchange_account_id,
        LedgerObservationQueryRow.deployment_environment == scope.deployment_environment,
    )
    basis_id = await session.scalar(
        select(AcceptedCapitalBasisRow.id)
        .join(LedgerObservationRow, LedgerObservationRow.id == AcceptedCapitalBasisRow.observation_id)
        .join(LedgerObservationQueryRow,
              LedgerObservationQueryRow.query_id == LedgerObservationRow.query_id)
        .where(*in_scope)
        .order_by(LedgerObservationQueryRow.query_revision.desc())
        .limit(1)
    )
    if basis_id is None:
        raise RefusedError("boot_basis_missing")
    now_ms = await session.scalar(
        select(LedgerObservationQueryRow.started_at_ms).where(*in_scope)
        .order_by(LedgerObservationQueryRow.query_revision.desc()).limit(1)
    )
    cells = (await session.execute(
        select(AcceptedCapitalBasisCellRow.symbol, AcceptedCapitalBasisCellRow.cell_id)
        .where(AcceptedCapitalBasisCellRow.basis_id == basis_id)
        .order_by(AcceptedCapitalBasisCellRow.symbol, AcceptedCapitalBasisCellRow.cell_id)
    )).all()
    reader = build_ledger_capital_reader()
    reads: list[dict[str, object]] = []
    for symbol, cell_id in cells:
        capital_scope = CapitalScope(scope.exchange_account_id, scope.deployment_environment,
                                     symbol, cell_id)
        try:
            read = await reader.read_capital(session, capital_scope, now_ms=now_ms,
                                             max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS)
        except Exception as exc:  # any refusal of the reader is a failed boot read
            raise RefusedError("boot_capital_read_failed") from exc
        result = read.result
        reason = None if isinstance(result, Available) else str(getattr(result, "reason", ""))
        if read.basis_id is None and reason != _PENDING:
            raise RefusedError("boot_capital_read_failed")
        reads.append({
            "symbol": symbol, "cell_id": cell_id,
            "basis_id": None if read.basis_id is None else str(read.basis_id),
            "result": "available" if reason is None else f"blocked:{reason}",
        })
    return str(basis_id), reads


async def _check(database_url: str) -> dict[str, object]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from bfx_funding_bot.apps.authority_support import require_ledger_seed
    from bfx_funding_bot.core.authority import AuthorityMismatch, read_authority
    from bfx_funding_bot.core.database_realm import DatabaseRealmMismatch, read_database_realm
    from bfx_funding_bot.core.schema_head import (
        SchemaHeadMismatch,
        assert_schema_head,
        build_head,
    )

    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            try:
                await assert_schema_head(session)
            except SchemaHeadMismatch:
                raise RefusedError("boot_schema_head_mismatch") from None
            try:
                realm = await read_database_realm(session)
            except DatabaseRealmMismatch:
                raise RefusedError("boot_realm_mismatch") from None
            try:
                authority = await read_authority(session, supported=frozenset({"ledger"}))
            except AuthorityMismatch:
                raise RefusedError("boot_authority_not_ledger") from None
            scopes = await _scopes(session)
            if not scopes:
                raise RefusedError("boot_ledger_empty")
            if any(scope.deployment_environment != realm for scope in scopes):
                raise RefusedError("boot_realm_mismatch")
            try:
                await require_ledger_seed(session, venue=VENUE, scopes=tuple(scopes))
            except AuthorityMismatch:
                raise RefusedError("boot_seed_missing") from None
            checked: list[dict[str, object]] = []
            for scope in scopes:
                basis_id, reads = await _reads(session, scope)
                checked.append({
                    "exchange_account_id": str(scope.exchange_account_id),
                    "deployment_environment": scope.deployment_environment,
                    "basis_id": basis_id, "reads": reads,
                })
            if not any(item["reads"] for item in checked):
                raise RefusedError("boot_capital_unread")
    finally:
        await engine.dispose()
    return {"boot": {"schema_head": build_head(), "realm": realm, "authority": authority,
                     "scopes": checked}}


def main() -> int:
    try:
        result = asyncio.run(_check(os.environ["DATABASE_URL"]))
    except RefusedError as exc:
        print(json.dumps({"error": str(exc)}))
        return EXIT_REFUSED
    except Exception as exc:  # an unexpected failure is reported by type only
        print(json.dumps({"error": "boot_check_failed", "type": type(exc).__name__}))
        return EXIT_REFUSED
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
