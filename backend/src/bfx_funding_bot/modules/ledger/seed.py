"""Facade of the one-time legacy closure seed (S1-4d, owner only, dormant).

``write_seed`` plans every row from the closure, states the per-table digests it expects
(``table_digest.digest_rows`` over the planned rows, plus the unchanged epoch table), then
writes the rows in the caller's transaction. ``verify_seed`` recomputes the digests from the
database (``table_digest.digest_ledger``, which needs the transaction switched to READ ONLY
after the writes) and lists every table whose count, bytes or watermark differ. The caller
(``apps/ledger_seed.py``) commits only on an empty list.

The switch (``--switch``) appends the ``ledger`` epoch in the same transaction, after the
writes and before the verification: ``switch_epoch_row`` plans the row (next ``epoch_seq``),
``append_switch_epoch`` writes it, and ``with_epoch`` replaces every scope's expected epoch
digest with the digest of the prior rows plus that row, so the read-back covers it too.

Never called by a runtime role: the schema refuses a ``legacy_seed`` observation and a
policy-less attempt from any role but the table owner, and the epoch trigger refuses every
ledger write from runtime roles until the switch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import Scope, SeedClosure
from bfx_funding_bot.modules.ledger._internal.seed import (
    SeedPlan,
    ledger_rows_in_scope,
    plan_seed,
    write_plan,
)
from bfx_funding_bot.modules.ledger.table_digest import (
    CANONICAL_COLUMNS,
    DIGEST_TABLES,
    TableDigest,
    digest_ledger,
    digest_rows,
)
from bfx_funding_bot.modules.ledger.tables import CapitalAuthorityEpochRow

EPOCH_TABLE = CapitalAuthorityEpochRow.__tablename__
SWITCH_AUTHORITY = "ledger"


@dataclass(frozen=True, slots=True)
class SeedResult:
    scope: Scope
    query_id: UUID
    observation_id: UUID
    basis_id: UUID
    attempt_seq_high_water: int
    expected: Mapping[str, TableDigest]


@dataclass(frozen=True, slots=True)
class DigestMismatch:
    table: str
    expected: TableDigest
    actual: TableDigest


async def epoch_rows(session: AsyncSession) -> tuple[dict[str, object], ...]:
    """The (global) epoch table's canonical rows in this snapshot (before any switch append)."""
    epoch = CapitalAuthorityEpochRow.__table__
    rows = await session.execute(
        select(*(epoch.c[name] for name in CANONICAL_COLUMNS[EPOCH_TABLE]))
    )
    return tuple(dict(row._mapping) for row in rows)


def expected_digests(
    plan: SeedPlan, epoch: tuple[dict[str, object], ...]
) -> dict[str, TableDigest]:
    """Every digest table of the scope as the plan will leave it (an empty table included)."""
    return {
        name: digest_rows(name, epoch) if name == EPOCH_TABLE
        else digest_rows(name, plan.table_rows(name), scope=plan.scope)
        for name in DIGEST_TABLES
    }


async def write_seed(session: AsyncSession, closure: SeedClosure) -> SeedResult:
    """Plan, state the expected digests, then write the seed in the caller's transaction."""
    plan = plan_seed(closure)
    expected = expected_digests(plan, await epoch_rows(session))
    await write_plan(session, plan)
    (basis,) = plan.table_rows("accepted_capital_basis")
    high_water = basis["attempt_seq_high_water"]
    assert isinstance(high_water, int)
    return SeedResult(
        plan.scope, plan.query_id, plan.observation_id, plan.basis_id, high_water, expected
    )


def switch_epoch_row(
    prior: tuple[dict[str, object], ...], *, now_ms: int, actor: str, reason: str,
    evidence: dict[str, object],
) -> dict[str, object]:
    """The ``ledger`` epoch row the switch appends after ``prior`` (the snapshot's rows)."""
    latest = max((int(str(row["epoch_seq"])) for row in prior), default=0)
    return {"epoch_seq": latest + 1, "authority": SWITCH_AUTHORITY, "set_at_ms": now_ms,
            "actor": actor, "reason": reason, "evidence": evidence}


async def append_switch_epoch(session: AsyncSession, row: dict[str, object]) -> None:
    """Write the planned epoch row in the caller's (seed) transaction; owner only."""
    await session.execute(insert(CapitalAuthorityEpochRow).values(**row))


def with_epoch(
    expected: Mapping[str, TableDigest], prior: tuple[dict[str, object], ...],
    row: dict[str, object],
) -> dict[str, TableDigest]:
    """``expected`` with the epoch table as it is after ``row`` was appended to ``prior``."""
    return {**expected, EPOCH_TABLE: digest_rows(EPOCH_TABLE, (*prior, row))}


async def verify_seed(
    session: AsyncSession, scope: Scope, expected: Mapping[str, TableDigest]
) -> tuple[DigestMismatch, ...]:
    """Recompute every digest from the database; the tables that differ from ``expected``.

    The session must be REPEATABLE READ READ ONLY (``SET TRANSACTION READ ONLY`` after the
    writes keeps their snapshot and makes ``digest_ledger`` accept it).
    """
    actual = await digest_ledger(session, scope=scope)
    mismatches: list[DigestMismatch] = []
    for digest in actual.tables:
        wanted = expected.get(digest.table)
        if wanted is None or (wanted.count, wanted.sha256, wanted.watermark) != (
            digest.count, digest.sha256, digest.watermark
        ):
            assert wanted is not None, digest.table
            mismatches.append(DigestMismatch(digest.table, wanted, digest))
    return tuple(mismatches)


__all__ = [
    "EPOCH_TABLE",
    "SWITCH_AUTHORITY",
    "DigestMismatch",
    "SeedPlan",
    "SeedResult",
    "append_switch_epoch",
    "epoch_rows",
    "expected_digests",
    "ledger_rows_in_scope",
    "plan_seed",
    "switch_epoch_row",
    "verify_seed",
    "with_epoch",
    "write_plan",
    "write_seed",
]
