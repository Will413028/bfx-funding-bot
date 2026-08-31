"""Make the Halt 1 ExchangeAccount identity contract mandatory.

The preceding revision is intentionally additive.  This revision is the
irreversible schema boundary: it refuses to change the database until every
account-scoped row has a mapped, existing ``exchange_accounts.id`` and the
legacy scaffold tables are empty.  Secret decryption, credential
re-encryption, and UUID backfill belong to ``cutover_identity.py`` and must
already have completed before this migration is invoked.

The old ``account_id`` text columns are retained as immutable migration/audit
provenance for this halt.  Runtime code must use the UUID column; the next
execution-state workstream can remove the provenance columns after the
forward-only cutover has been proven in production.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9b2c3d4e5f6a"
down_revision: str | Sequence[str] | None = "8a1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Keep this list explicit.  It is the reviewable allow-list for the contract
# boundary; adding a new money table requires adding it here and to the
# preceding additive migration in the same change.
_MONEY_TABLES: tuple[str, ...] = (
    "event_log",
    "offer_claims",
    "position_state",
    "reconcile_observation",
    "execution_decisions",
    "diagnostics",
    "nav_peak",
    "trading_halt",
    "attribution_weekly",
    "config_regime",
)
_ACCOUNT_SCOPED_TABLES: tuple[str, ...] = (*_MONEY_TABLES, "api_keys", "user_configs")
_LEGACY_ZERO_TABLES: tuple[str, ...] = ("users", "executions", "billing_records")

# Every key whose legacy text realm is replaced by the UUID owner must be
# checked before DDL.  The optional predicate mirrors the partial unique
# indexes below; NULL values are intentionally excluded because PostgreSQL's
# unique indexes permit more than one NULL.
_IDENTITY_KEY_GROUPS: dict[str, tuple[str, tuple[str, ...], str | None]] = {
    "event_log.dedup": (
        "event_log",
        (
            "exchange_account_id",
            "deployment_environment",
            "event_type",
            "venue_offer_id",
            "venue_seq",
        ),
        "venue_offer_id IS NOT NULL AND venue_seq IS NOT NULL",
    ),
    "offer_claims.primary_key": (
        "offer_claims",
        ("exchange_account_id", "deployment_environment", "cid"),
        None,
    ),
    "offer_claims.venue_offer_id": (
        "offer_claims",
        ("exchange_account_id", "deployment_environment", "venue_offer_id"),
        "venue_offer_id IS NOT NULL",
    ),
    "offer_claims.execution_decision_id": (
        "offer_claims",
        ("exchange_account_id", "deployment_environment", "execution_decision_id"),
        "execution_decision_id IS NOT NULL",
    ),
    "position_state.primary_key": (
        "position_state",
        ("exchange_account_id", "deployment_environment", "symbol"),
        None,
    ),
    "nav_peak.primary_key": (
        "nav_peak",
        ("exchange_account_id", "deployment_environment", "symbol"),
        None,
    ),
    "attribution_weekly.primary_key": (
        "attribution_weekly",
        ("deployment_environment", "exchange_account_id", "cell", "week_start_ms"),
        None,
    ),
    "config_regime.primary_key": (
        "config_regime",
        ("deployment_environment", "exchange_account_id", "recorded_at_ms"),
        None,
    ),
}


def _count(bind: sa.Connection, statement: str) -> int:
    return int(bind.execute(sa.text(statement)).scalar_one() or 0)


def _distinct_unmapped_realms(bind: sa.Connection, table_name: str) -> set[str]:
    rows = bind.execute(
        sa.text(
            f"SELECT DISTINCT account_id FROM {table_name} "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM legacy_account_realm_map map "
            f"  WHERE map.realm_key = {table_name}.account_id"
            ")"
        )
    )
    return {str(row[0]) for row in rows if row[0] is not None}


def _format_preflight_errors(
    *,
    null_rows: dict[str, int],
    unmapped_realms: Iterable[str],
    orphan_rows: dict[str, int],
    nonzero_legacy_tables: dict[str, int],
    identity_collisions: dict[str, tuple[str, ...]] | None = None,
    invalid_active_credentials: int = 0,
) -> str:
    """Render deterministic, operator-actionable contract blockers.

    This is deliberately a pure function so the wording and all blocking
    categories can be covered without a live PostgreSQL instance.
    """
    sections: list[str] = []
    if null_rows:
        sections.append(
            "NULL exchange_account_id: "
            + ", ".join(f"{name}={null_rows[name]}" for name in sorted(null_rows))
        )
    realms = sorted(set(unmapped_realms))
    if realms:
        sections.append("unmapped legacy realms: " + ", ".join(realms))
    if orphan_rows:
        sections.append(
            "orphan exchange_account_id: "
            + ", ".join(f"{name}={orphan_rows[name]}" for name in sorted(orphan_rows))
        )
    if nonzero_legacy_tables:
        sections.append(
            "legacy tables must be empty: "
            + ", ".join(
                f"{name}={nonzero_legacy_tables[name]}"
                for name in sorted(nonzero_legacy_tables)
            )
        )
    if identity_collisions:
        sections.append(
            "UUID key collisions: "
            + "; ".join(
                f"{name}=[{', '.join(identity_collisions[name])}]"
                for name in sorted(identity_collisions)
            )
        )
    if invalid_active_credentials:
        sections.append(
            "active credentials lack successful verification: "
            f"count={invalid_active_credentials}"
        )
    if not sections:
        return ""
    return "contract migration blocked: " + "; ".join(sections)


def _identity_key_collisions(bind: sa.Connection) -> dict[str, tuple[str, ...]]:
    """Return deterministic samples of rows that would collide after rekeying."""
    collisions: dict[str, tuple[str, ...]] = {}
    for name, (table_name, columns, predicate) in _IDENTITY_KEY_GROUPS.items():
        column_sql = ", ".join(columns)
        predicates = ["exchange_account_id IS NOT NULL"]
        if predicate:
            predicates.append(predicate)
        where_sql = " WHERE " + " AND ".join(predicates)
        rows = bind.execute(
            sa.text(
                f"SELECT {column_sql}, count(*) AS duplicate_count "
                f"FROM {table_name}{where_sql} "
                f"GROUP BY {column_sql} "
                "HAVING count(*) > 1 "
                f"ORDER BY {column_sql} LIMIT 20"
            )
        )
        samples: list[str] = []
        for row in rows:
            values = [f"{column}={row[index]!s}" for index, column in enumerate(columns)]
            values.append(f"count={row[len(columns)]}")
            samples.append(",".join(values))
        if samples:
            collisions[name] = tuple(samples)
    return collisions


def _assert_contract_preconditions(bind: sa.Connection) -> None:
    """Fail before any DDL if cutover evidence is incomplete."""
    null_rows: dict[str, int] = {}
    orphan_rows: dict[str, int] = {}
    unmapped_realms: set[str] = set()

    for table_name in _ACCOUNT_SCOPED_TABLES:
        null_count = _count(
            bind,
            f"SELECT count(*) FROM {table_name} "
            "WHERE exchange_account_id IS NULL",
        )
        if null_count:
            null_rows[table_name] = null_count

        orphan_count = _count(
            bind,
            f"SELECT count(*) FROM {table_name} rows "
            "WHERE rows.exchange_account_id IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM exchange_accounts account "
            "  WHERE account.id = rows.exchange_account_id"
            ")",
        )
        if orphan_count:
            orphan_rows[table_name] = orphan_count

    for table_name in _MONEY_TABLES:
        unmapped_realms.update(
            f"{table_name}:{realm}"
            for realm in _distinct_unmapped_realms(bind, table_name)
        )

    nonzero_legacy_tables = {
        table_name: count
        for table_name in _LEGACY_ZERO_TABLES
        if (count := _count(bind, f"SELECT count(*) FROM {table_name}"))
    }
    invalid_active_credentials = _count(
        bind,
        "SELECT count(*) FROM exchange_account_credentials "
        "WHERE lifecycle_status = 'active' "
        "AND (verified_at IS NULL OR last_verify_error IS NOT NULL)",
    )
    identity_collisions = _identity_key_collisions(bind)

    message = _format_preflight_errors(
        null_rows=null_rows,
        unmapped_realms=unmapped_realms,
        orphan_rows=orphan_rows,
        nonzero_legacy_tables=nonzero_legacy_tables,
        identity_collisions=identity_collisions,
        invalid_active_credentials=invalid_active_credentials,
    )
    if message:
        raise RuntimeError(message)


def _set_uuid_not_null() -> None:
    for table_name in _ACCOUNT_SCOPED_TABLES:
        op.alter_column(
            table_name,
            "exchange_account_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=False,
        )


def _harden_credential_lifecycle() -> None:
    op.drop_constraint(
        "ck_exchange_account_credentials_lifecycle_status",
        "exchange_account_credentials",
        type_="check",
    )
    op.drop_constraint(
        "ck_exchange_account_credentials_active_verified",
        "exchange_account_credentials",
        type_="check",
    )
    op.create_check_constraint(
        "ck_exchange_account_credentials_lifecycle_status",
        "exchange_account_credentials",
        "lifecycle_status IN ('pending', 'active', 'revoked', 'retired')",
    )
    op.create_check_constraint(
        "ck_exchange_account_credentials_active_verified",
        "exchange_account_credentials",
        "lifecycle_status <> 'active' OR "
        "(verified_at IS NOT NULL AND last_verify_error IS NULL)",
    )
    op.alter_column(
        "exchange_account_credentials",
        "lifecycle_status",
        existing_type=sa.Text(),
        server_default=sa.text("'pending'"),
    )


def _add_restrictive_foreign_keys() -> None:
    for table_name in _ACCOUNT_SCOPED_TABLES:
        op.create_foreign_key(
            f"fk_{table_name}_exchange_account",
            table_name,
            "exchange_accounts",
            ["exchange_account_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def _replace_account_indexes_and_keys() -> None:
    # The additive revision created short-lived UUID+environment indexes to
    # make backfill/verification cheap.  The canonical indexes below replace
    # them; keeping both would only add write amplification after cutover.
    for table_name in _MONEY_TABLES:
        op.drop_index(f"idx_{table_name}_exchange_account_env", table_name=table_name)

    # event_log: both the read path and venue dedup key must be account UUID
    # scoped.  Keep the stable names so operational dashboards do not need a
    # second rename during the cutover.
    op.drop_index("idx_event_log_acct_env_seq", table_name="event_log")
    op.create_index(
        "idx_event_log_acct_env_seq",
        "event_log",
        ["exchange_account_id", "deployment_environment", "event_seq"],
    )
    op.drop_index("uq_event_log_dedup", table_name="event_log")
    op.create_index(
        "uq_event_log_dedup",
        "event_log",
        [
            "exchange_account_id",
            "deployment_environment",
            "event_type",
            "venue_offer_id",
            "venue_seq",
        ],
        unique=True,
    )

    # Snapshot identities must not collide when two legacy realms used the
    # same CID/symbol.  Partial uniques retain the historical NULL semantics.
    op.drop_index("uq_offer_claims_venue_offer_id", table_name="offer_claims")
    op.create_index(
        "uq_offer_claims_venue_offer_id",
        "offer_claims",
        ["exchange_account_id", "deployment_environment", "venue_offer_id"],
        unique=True,
        postgresql_where=sa.text("venue_offer_id IS NOT NULL"),
    )
    op.drop_index("uq_offer_claims_execution_decision_id", table_name="offer_claims")
    op.create_index(
        "uq_offer_claims_execution_decision_id",
        "offer_claims",
        ["exchange_account_id", "deployment_environment", "execution_decision_id"],
        unique=True,
        postgresql_where=sa.text("execution_decision_id IS NOT NULL"),
    )
    op.drop_constraint("offer_claims_pkey", "offer_claims", type_="primary")
    op.create_primary_key(
        "offer_claims_pkey",
        "offer_claims",
        ["exchange_account_id", "deployment_environment", "cid"],
    )

    for table_name, columns in (
        (
            "position_state",
            ["exchange_account_id", "deployment_environment", "symbol"],
        ),
        ("nav_peak", ["exchange_account_id", "deployment_environment", "symbol"]),
        (
            "attribution_weekly",
            ["deployment_environment", "exchange_account_id", "cell", "week_start_ms"],
        ),
        (
            "config_regime",
            ["deployment_environment", "exchange_account_id", "recorded_at_ms"],
        ),
    ):
        op.drop_constraint(f"{table_name}_pkey", table_name, type_="primary")
        op.create_primary_key(f"{table_name}_pkey", table_name, columns)

    op.drop_index("idx_reconcile_obs_acct_env_symbol_id", table_name="reconcile_observation")
    op.create_index(
        "idx_reconcile_obs_acct_env_symbol_id",
        "reconcile_observation",
        ["exchange_account_id", "deployment_environment", "symbol", "id"],
    )
    op.drop_index("idx_diagnostics_acct_occurred", table_name="diagnostics")
    op.create_index(
        "idx_diagnostics_acct_occurred",
        "diagnostics",
        ["exchange_account_id", "occurred_at"],
    )
    op.drop_index("ix_trading_halt_realm_id", table_name="trading_halt")
    op.create_index(
        "ix_trading_halt_realm_id",
        "trading_halt",
        ["exchange_account_id", "deployment_environment", "id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_contract_preconditions(bind)
    _set_uuid_not_null()
    _harden_credential_lifecycle()
    _add_restrictive_foreign_keys()
    _replace_account_indexes_and_keys()

    # These are the pre-product scaffold tables.  They are removed only after
    # the zero-row checks above; a non-empty table aborts the whole transaction.
    # Drop children first because their historical FK constraints point at
    # ``users``.
    op.drop_table("executions")
    op.drop_table("billing_records")
    op.drop_table("users")


def downgrade() -> None:
    raise RuntimeError(
        "contract identity migration is forward-only; restore the verified "
        "pre-cutover backup/PITR and reconcile the venue instead of downgrading"
    )
