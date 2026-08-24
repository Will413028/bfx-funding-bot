"""enforce unique non-null offer claim reservation identities.

Revision ID: a6c9e2f4b7d1
Revises: f5b8d0e2f3a4
Create Date: 2026-08-23 00:00:00.000000
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import context, op

revision: str = "a6c9e2f4b7d1"
down_revision: str | Sequence[str] | None = "f5b8d0e2f3a4"
branch_labels = None
depends_on = None


class _ClaimIdentityRow(NamedTuple):
    account_id: str
    deployment_environment: str
    cid: int
    venue_offer_id: str | None
    execution_decision_id: str | None


class _IdentityCollision(NamedTuple):
    field: str
    account_id: str
    deployment_environment: str
    identity: str
    rows: tuple[_ClaimIdentityRow, ...]


class OfferClaimIdentityMigrationError(RuntimeError):
    """Existing projections violate the reservation identities being enforced."""


_POSTGRES_PREFLIGHT_LOCK_SQL = "LOCK TABLE offer_claims IN SHARE ROW EXCLUSIVE MODE"


def upgrade() -> None:
    if context.is_offline_mode():
        # ``alembic upgrade --sql`` cannot inspect rows in Python.  Emit the
        # same lock + fail-closed check into the generated PostgreSQL script so
        # an offline apply still stops before either index and reports every
        # collision deterministically.  The lock closes the gap between
        # preflight and CREATE INDEX by blocking concurrent claim writes.
        op.execute(sa.text(_POSTGRES_PREFLIGHT_LOCK_SQL))
        op.execute(sa.text(_OFFLINE_PREFLIGHT_SQL))
    else:
        connection = op.get_bind()
        _lock_offer_claims_for_identity_preflight(connection)
        _assert_no_identity_collisions(connection)
    # PostgreSQL runs this revision in one DDL transaction.  Do not use
    # CONCURRENTLY or commit between indexes: if either CREATE fails, both the
    # first index and this revision roll back together.
    op.create_index(
        "uq_offer_claims_venue_offer_id",
        "offer_claims",
        ["account_id", "deployment_environment", "venue_offer_id"],
        unique=True,
        postgresql_where=sa.text("venue_offer_id IS NOT NULL"),
        sqlite_where=sa.text("venue_offer_id IS NOT NULL"),
    )
    op.create_index(
        "uq_offer_claims_execution_decision_id",
        "offer_claims",
        ["account_id", "deployment_environment", "execution_decision_id"],
        unique=True,
        postgresql_where=sa.text("execution_decision_id IS NOT NULL"),
        sqlite_where=sa.text("execution_decision_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_offer_claims_execution_decision_id", table_name="offer_claims")
    op.drop_index("uq_offer_claims_venue_offer_id", table_name="offer_claims")


def _assert_no_identity_collisions(connection: Connection) -> None:
    rows = tuple(
        _ClaimIdentityRow(
            account_id=str(row.account_id),
            deployment_environment=str(row.deployment_environment),
            cid=int(row.cid),
            venue_offer_id=(str(row.venue_offer_id) if row.venue_offer_id is not None else None),
            execution_decision_id=(
                str(row.execution_decision_id)
                if row.execution_decision_id is not None
                else None
            ),
        )
        for row in connection.execute(
            sa.text(
                """
                SELECT
                    account_id,
                    deployment_environment,
                    cid,
                    venue_offer_id,
                    execution_decision_id
                FROM offer_claims
                ORDER BY account_id, deployment_environment, cid
                """
            )
        )
    )
    collisions = _find_identity_collisions(rows)
    if collisions:
        raise OfferClaimIdentityMigrationError(_format_collision_diagnostic(collisions))


def _lock_offer_claims_for_identity_preflight(connection: Connection) -> None:
    """Prevent writes from racing a PostgreSQL identity preflight."""
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text(_POSTGRES_PREFLIGHT_LOCK_SQL))


def _find_identity_collisions(
    rows: tuple[_ClaimIdentityRow, ...],
) -> tuple[_IdentityCollision, ...]:
    collisions: list[_IdentityCollision] = []
    for field in ("venue_offer_id", "execution_decision_id"):
        grouped: dict[tuple[str, str, str], list[_ClaimIdentityRow]] = defaultdict(list)
        for row in rows:
            identity = getattr(row, field)
            if identity is not None:
                grouped[(row.account_id, row.deployment_environment, identity)].append(row)
        for (account_id, environment, identity), matching_rows in sorted(grouped.items()):
            if len(matching_rows) > 1:
                collisions.append(
                    _IdentityCollision(
                        field=field,
                        account_id=account_id,
                        deployment_environment=environment,
                        identity=identity,
                        rows=tuple(sorted(matching_rows, key=lambda row: row.cid)),
                    )
                )
    return tuple(collisions)


def _format_collision_diagnostic(collisions: tuple[_IdentityCollision, ...]) -> str:
    lines = [
        f"migration {revision} blocked by {len(collisions)} "
        "offer_claims identity collision(s):",
    ]
    for collision in collisions:
        lines.append(
            f"- account_id={collision.account_id!r} "
            f"deployment_environment={collision.deployment_environment!r} "
            f"{collision.field}={collision.identity!r}",
        )
        lines.extend(
            "  "
            f"cid={row.cid} venue_offer_id={row.venue_offer_id!r} "
            f"execution_decision_id={row.execution_decision_id!r}"
            for row in collision.rows
        )
    lines.append(
        "Remediation: inspect event_log and venue truth, then explicitly quarantine or correct "
        "the conflicting offer_claims projections before rerunning "
        "`cd backend_py && uv run alembic upgrade head`. This migration does not merge, "
        "delete, or quarantine rows.",
    )
    return "\n".join(lines)


_OFFLINE_PREFLIGHT_SQL = r"""
DO $offer_claim_identity_preflight$
DECLARE
    collision_count integer;
    collision_details text;
BEGIN
    WITH identity_rows AS (
        SELECT
            1 AS kind_order,
            'venue_offer_id'::text AS kind,
            account_id,
            deployment_environment,
            venue_offer_id AS identity,
            cid,
            venue_offer_id,
            execution_decision_id
        FROM offer_claims
        WHERE venue_offer_id IS NOT NULL
        UNION ALL
        SELECT
            2 AS kind_order,
            'execution_decision_id'::text AS kind,
            account_id,
            deployment_environment,
            execution_decision_id AS identity,
            cid,
            venue_offer_id,
            execution_decision_id
        FROM offer_claims
        WHERE execution_decision_id IS NOT NULL
    ),
    collision_keys AS (
        SELECT kind_order, kind, account_id, deployment_environment, identity
        FROM identity_rows
        GROUP BY kind_order, kind, account_id, deployment_environment, identity
        HAVING count(*) > 1
    ),
    collision_groups AS (
        SELECT
            keys.kind_order,
            keys.kind,
            keys.account_id,
            keys.deployment_environment,
            keys.identity,
            string_agg(
                format(
                    'cid=%s venue_offer_id=%L execution_decision_id=%L',
                    rows.cid,
                    rows.venue_offer_id,
                    rows.execution_decision_id
                ),
                '; ' ORDER BY rows.cid
            ) AS conflicting_rows
        FROM collision_keys AS keys
        JOIN identity_rows AS rows
          ON rows.kind = keys.kind
         AND rows.account_id = keys.account_id
         AND rows.deployment_environment = keys.deployment_environment
         AND rows.identity = keys.identity
        GROUP BY
            keys.kind_order,
            keys.kind,
            keys.account_id,
            keys.deployment_environment,
            keys.identity
    )
    SELECT
        count(*)::integer,
        string_agg(
            format(
                '- account_id=%L deployment_environment=%L %s=%L: %s',
                account_id,
                deployment_environment,
                kind,
                identity,
                conflicting_rows
            ),
            E'\n' ORDER BY kind_order, account_id, deployment_environment, identity
        )
    INTO collision_count, collision_details
    FROM collision_groups;

    IF collision_count > 0 THEN
        RAISE EXCEPTION USING MESSAGE = format(
            'migration a6c9e2f4b7d1 blocked by %s offer_claims identity collision(s):%s%s%s%s',
            collision_count,
            E'\n',
            collision_details,
            E'\n',
            'Remediation: inspect event_log and venue truth, then explicitly quarantine or ' ||
            'correct conflicting offer_claims rows before rerunning `cd backend_py && uv run ' ||
            'alembic upgrade head`. This migration does not merge, delete, or quarantine rows.'
        );
    END IF;
END
$offer_claim_identity_preflight$
"""
