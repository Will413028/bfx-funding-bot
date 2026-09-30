"""Record venue facts only on the accepted capital basis.

Policy is applied per symbol at read time, so the basis drops its single policy
revision and records the attempt sequence it covers. Blocks move to the symbol
rows (plus a scope-wide block); the legacy credit-cell flag goes. Funding trades
become observation evidence, wallets carry their funding symbol, attempts carry
their decision's cell (trigger-checked), and only quarantines take a manual
resolution.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
_TRADE_COLUMNS = (
    "observation_id",
    "trade_id",
    "symbol",
    "venue_offer_id",
    "amount",
    "rate",
    "period_days",
    "mts_create",
    "maker",
)
# Columns this revision adds to existing tables (all readable by the reader group).
_ADDED = {
    "ledger_observation": (
        "trades_complete",
        "trades_requested_start_ms",
        "trades_requested_end_ms",
    ),
    "ledger_observation_wallet": ("symbol",),
    "submission_attempt_journal": ("cell_id",),
    "accepted_capital_basis": ("attempt_seq_high_water", "scope_block"),
    "accepted_capital_basis_symbol": ("block",),
}
# Columns this revision drops; downgrade restores them with c3d4e5f6a7b8's grants.
_DROPPED = {
    "accepted_capital_basis": ("policy_revision_id", "authorization_block", "credit_cells_present"),
}
_POPULATED_GUARD = (
    "ledger_observation",
    "submission_attempt_journal",
    "execution_resolution_journal",
    "accepted_capital_basis",
)
_ACCEPTANCE_V1 = (
    "NOT accepted OR (wallets_complete AND "
    "offers_complete AND credits_complete AND loans_complete AND "
    "offer_history_complete AND credit_history_complete)"
)
_ACCEPTANCE_V2 = (
    "NOT accepted OR (wallets_complete AND "
    "offers_complete AND credits_complete AND loans_complete AND "
    "offer_history_complete AND credit_history_complete AND trades_complete)"
)

# guard_ledger_scope: the attempt branch also requires the decision's cell_id.
_GUARD_SCOPE = """CREATE OR REPLACE FUNCTION public.guard_ledger_scope() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE parent_scope record;
        BEGIN
          IF TG_TABLE_NAME = 'submission_attempt_journal' THEN
            SELECT exchange_account_id, deployment_environment, symbol, cell_id
              INTO parent_scope
              FROM public.execution_decisions WHERE decision_id = NEW.execution_decision_id;
            IF parent_scope.exchange_account_id IS NULL
               OR (parent_scope.exchange_account_id,
                   parent_scope.deployment_environment, parent_scope.symbol,
                   parent_scope.cell_id)
                  IS DISTINCT FROM (NEW.exchange_account_id,
                    NEW.deployment_environment, NEW.symbol, NEW.cell_id) THEN
              RAISE EXCEPTION 'ledger decision scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'quarantine_member' THEN
            SELECT q.exchange_account_id, q.deployment_environment, q.symbol INTO parent_scope
              FROM public.quarantine_opening q WHERE q.quarantine_id = NEW.quarantine_id;
            IF NOT EXISTS (
              SELECT 1 FROM public.ledger_observation o
              JOIN (
                SELECT observation_id, 'offer'::text AS source_kind,
                       venue_offer_id AS venue_object_id, symbol
                  FROM public.ledger_observation_offer
                UNION ALL
                SELECT observation_id, 'offer'::text, venue_offer_id, symbol
                  FROM public.ledger_observation_offer_history
                UNION ALL
                SELECT observation_id, source_kind, venue_credit_id, symbol
                  FROM public.ledger_observation_credit
                UNION ALL
                SELECT observation_id, source_kind, venue_credit_id, symbol
                  FROM public.ledger_observation_credit_history
              ) h ON h.observation_id = o.id
              WHERE o.id = NEW.observation_id
                AND h.source_kind = NEW.source_kind
                AND h.venue_object_id = NEW.venue_object_id
                AND (o.exchange_account_id, o.deployment_environment, h.symbol)
                    = (parent_scope.exchange_account_id,
                       parent_scope.deployment_environment, parent_scope.symbol)) THEN
              RAISE EXCEPTION 'ledger member scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'execution_resolution_journal' THEN
            IF NEW.attempt_id IS NOT NULL THEN
              SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
                FROM public.submission_attempt_journal WHERE attempt_id = NEW.attempt_id;
            ELSE
              SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
                FROM public.quarantine_opening WHERE quarantine_id = NEW.quarantine_id;
            END IF;
            IF (parent_scope.exchange_account_id,
                parent_scope.deployment_environment, parent_scope.symbol)
                 IS DISTINCT FROM (NEW.exchange_account_id, NEW.deployment_environment, NEW.symbol)
               OR NOT EXISTS (SELECT 1 FROM public.ledger_observation o
                 WHERE o.id = NEW.observation_id
                 AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment)) THEN
              RAISE EXCEPTION 'ledger resolution scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'accepted_capital_basis' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o WHERE o.id = NEW.observation_id
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment)
              AND o.accept_revision = NEW.accept_revision) THEN
              RAISE EXCEPTION 'ledger basis scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_offer_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment))
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_offer_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_offer_id = NEW.venue_offer_id
                  AND h.symbol = NEW.symbol AND h.terminal_kind = NEW.terminal_kind
                  AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment))) THEN
              RAISE EXCEPTION 'ledger offer mirror scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_credit_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment))
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_credit_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_credit_id = NEW.venue_credit_id
                  AND h.source_kind = NEW.source_kind AND h.symbol = NEW.symbol
                  AND h.terminal_kind = NEW.terminal_kind
                  AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment))) THEN
              RAISE EXCEPTION 'ledger credit mirror scope mismatch';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""

# Exactly c3d4e5f6a7b8's upgrade body, restored on downgrade.
_GUARD_SCOPE_C3D4 = """CREATE OR REPLACE FUNCTION public.guard_ledger_scope() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE parent_scope record;
        BEGIN
          IF TG_TABLE_NAME = 'submission_attempt_journal' THEN
            SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
              FROM public.execution_decisions WHERE decision_id = NEW.execution_decision_id;
            IF parent_scope.exchange_account_id IS NULL
               OR (parent_scope.exchange_account_id,
                   parent_scope.deployment_environment, parent_scope.symbol)
                  IS DISTINCT FROM (NEW.exchange_account_id,
                    NEW.deployment_environment, NEW.symbol) THEN
              RAISE EXCEPTION 'ledger decision scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'quarantine_member' THEN
            SELECT q.exchange_account_id, q.deployment_environment, q.symbol INTO parent_scope
              FROM public.quarantine_opening q WHERE q.quarantine_id = NEW.quarantine_id;
            IF NOT EXISTS (
              SELECT 1 FROM public.ledger_observation o
              JOIN (
                SELECT observation_id, 'offer'::text AS source_kind,
                       venue_offer_id AS venue_object_id, symbol
                  FROM public.ledger_observation_offer
                UNION ALL
                SELECT observation_id, 'offer'::text, venue_offer_id, symbol
                  FROM public.ledger_observation_offer_history
                UNION ALL
                SELECT observation_id, source_kind, venue_credit_id, symbol
                  FROM public.ledger_observation_credit
                UNION ALL
                SELECT observation_id, source_kind, venue_credit_id, symbol
                  FROM public.ledger_observation_credit_history
              ) h ON h.observation_id = o.id
              WHERE o.id = NEW.observation_id
                AND h.source_kind = NEW.source_kind
                AND h.venue_object_id = NEW.venue_object_id
                AND (o.exchange_account_id, o.deployment_environment, h.symbol)
                    = (parent_scope.exchange_account_id,
                       parent_scope.deployment_environment, parent_scope.symbol)) THEN
              RAISE EXCEPTION 'ledger member scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'execution_resolution_journal' THEN
            IF NEW.attempt_id IS NOT NULL THEN
              SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
                FROM public.submission_attempt_journal WHERE attempt_id = NEW.attempt_id;
            ELSE
              SELECT exchange_account_id, deployment_environment, symbol INTO parent_scope
                FROM public.quarantine_opening WHERE quarantine_id = NEW.quarantine_id;
            END IF;
            IF (parent_scope.exchange_account_id,
                parent_scope.deployment_environment, parent_scope.symbol)
                 IS DISTINCT FROM (NEW.exchange_account_id, NEW.deployment_environment, NEW.symbol)
               OR NOT EXISTS (SELECT 1 FROM public.ledger_observation o
                 WHERE o.id = NEW.observation_id
                 AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment)) THEN
              RAISE EXCEPTION 'ledger resolution scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'accepted_capital_basis' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o WHERE o.id = NEW.observation_id
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment)
              AND o.accept_revision = NEW.accept_revision) THEN
              RAISE EXCEPTION 'ledger basis scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_offer_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment))
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_offer_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_offer_id = NEW.venue_offer_id
                  AND h.symbol = NEW.symbol AND h.terminal_kind = NEW.terminal_kind
                  AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment))) THEN
              RAISE EXCEPTION 'ledger offer mirror scope mismatch';
            END IF;
          ELSIF TG_TABLE_NAME = 'venue_credit_mirror' THEN
            IF NOT EXISTS (SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.last_accepted_observation_id AND o.accepted
              AND (o.exchange_account_id, o.deployment_environment)
                = (NEW.exchange_account_id, NEW.deployment_environment))
              OR (NEW.terminal_evidence_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.ledger_observation_credit_history h
                JOIN public.ledger_observation o ON o.id = h.observation_id
                WHERE h.id = NEW.terminal_evidence_id AND h.venue_credit_id = NEW.venue_credit_id
                  AND h.source_kind = NEW.source_kind AND h.symbol = NEW.symbol
                  AND h.terminal_kind = NEW.terminal_kind
                  AND (o.exchange_account_id, o.deployment_environment)
                    = (NEW.exchange_account_id, NEW.deployment_environment))) THEN
              RAISE EXCEPTION 'ledger credit mirror scope mismatch';
            END IF;
          END IF;
          RETURN NEW;
        END $$"""


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _refuse_populated(direction: str) -> None:
    for name in _POPULATED_GUARD:
        if op.get_bind().execute(text(f"SELECT EXISTS (SELECT 1 FROM public.{name})")).scalar():
            raise RuntimeError(f"refuse {direction} of populated ledger: {name}")


def _revoke_columns(table: str, columns: tuple[str, ...]) -> None:
    listed = ", ".join(columns)
    op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{table} FROM PUBLIC")
    for role in _ROLES:
        if _role_exists(role):
            op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{table} FROM {role}")


def _grant_reader(table: str, columns: tuple[str, ...]) -> None:
    if _role_exists("bfx_cutover_reader"):
        op.execute(f"GRANT SELECT ({', '.join(columns)}) ON public.{table} TO bfx_cutover_reader")


def upgrade() -> None:
    _refuse_populated("upgrade")
    op.drop_constraint("ck_ledger_observation_acceptance", "ledger_observation", type_="check")
    # BEGIN AUTOGEN-DDL-UPGRADE
    op.create_table(
        "ledger_observation_trade",
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("venue_offer_id", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("rate", sa.Numeric(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("mts_create", sa.BigInteger(), nullable=False),
        sa.Column("maker", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "trade_id >= 0 AND amount > 0 AND rate >= 0 AND period_days > 0 AND mts_create >= 0",
            name="ck_ledger_observation_trade_amount",
        ),
        sa.ForeignKeyConstraint(["observation_id"], ["ledger_observation.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("observation_id", "trade_id"),
    )
    op.add_column(
        "accepted_capital_basis",
        sa.Column("attempt_seq_high_water", sa.BigInteger(), nullable=False),
    )
    op.add_column(
        "accepted_capital_basis",
        sa.Column(
            "scope_block",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
    )
    op.drop_constraint(
        op.f("accepted_capital_basis_policy_revision_id_fkey"),
        "accepted_capital_basis",
        type_="foreignkey",
    )
    op.drop_column("accepted_capital_basis", "credit_cells_present")
    op.drop_column("accepted_capital_basis", "authorization_block")
    op.drop_column("accepted_capital_basis", "policy_revision_id")
    op.add_column(
        "accepted_capital_basis_symbol",
        sa.Column(
            "block",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_execution_resolution_venue_offer",
        "execution_resolution_journal",
        ["venue_offer_id"],
        unique=False,
        postgresql_where=sa.text("venue_offer_id IS NOT NULL"),
    )
    op.add_column("ledger_observation", sa.Column("trades_complete", sa.Boolean(), nullable=False))
    op.add_column(
        "ledger_observation", sa.Column("trades_requested_start_ms", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "ledger_observation", sa.Column("trades_requested_end_ms", sa.BigInteger(), nullable=True)
    )
    op.add_column("ledger_observation_wallet", sa.Column("symbol", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_ledger_observation_wallet_symbol",
        "ledger_observation_wallet",
        ["observation_id", "symbol"],
    )
    op.add_column("submission_attempt_journal", sa.Column("cell_id", sa.Text(), nullable=False))
    op.create_index(
        "ix_transport_outcome_venue_offer",
        "transport_outcome_journal",
        ["venue_offer_id"],
        unique=False,
        postgresql_where=sa.text("venue_offer_id IS NOT NULL"),
    )
    # Autogenerate does not emit CHECK changes on existing tables.
    op.create_check_constraint(
        "ck_ledger_observation_acceptance", "ledger_observation", _ACCEPTANCE_V2
    )
    op.create_check_constraint(
        "ck_ledger_observation_trade_range",
        "ledger_observation",
        "(trades_requested_start_ms IS NULL) = (trades_requested_end_ms IS NULL) AND "
        "(trades_requested_start_ms IS NULL OR (trades_requested_start_ms >= 0 AND "
        "trades_requested_end_ms >= trades_requested_start_ms))",
    )
    op.create_check_constraint(
        "ck_execution_resolution_manual",
        "execution_resolution_journal",
        "action <> 'manual' OR quarantine_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_accepted_basis_high_water",
        "accepted_capital_basis",
        "attempt_seq_high_water >= 0",
    )
    # END AUTOGEN-DDL-UPGRADE

    op.execute(_GUARD_SCOPE)
    op.execute("REVOKE ALL ON FUNCTION public.guard_ledger_scope() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER immutable_ledger_write BEFORE UPDATE OR DELETE "
        "ON public.ledger_observation_trade "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_ledger_mutation()"
    )
    op.execute(
        "CREATE TRIGGER immutable_ledger_truncate BEFORE TRUNCATE "
        "ON public.ledger_observation_trade "
        "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_ledger_mutation()"
    )
    op.execute("REVOKE ALL ON TABLE public.ledger_observation_trade FROM PUBLIC")
    for role in _ROLES:
        if _role_exists(role):
            op.execute(f"REVOKE ALL ON TABLE public.ledger_observation_trade FROM {role}")
    _revoke_columns("ledger_observation_trade", _TRADE_COLUMNS)
    for table, columns in _ADDED.items():
        _revoke_columns(table, columns)
    if _role_exists("bfx_bot"):
        op.execute("GRANT SELECT, INSERT ON public.ledger_observation_trade TO bfx_bot")
    _grant_reader("ledger_observation_trade", _TRADE_COLUMNS)
    for table, columns in _ADDED.items():
        _grant_reader(table, columns)


def downgrade() -> None:
    _refuse_populated("downgrade")
    op.execute(_GUARD_SCOPE_C3D4)
    op.execute("REVOKE ALL ON FUNCTION public.guard_ledger_scope() FROM PUBLIC")
    for name in (
        ("ck_accepted_basis_high_water", "accepted_capital_basis"),
        ("ck_execution_resolution_manual", "execution_resolution_journal"),
        ("ck_ledger_observation_trade_range", "ledger_observation"),
        ("ck_ledger_observation_acceptance", "ledger_observation"),
    ):
        op.drop_constraint(name[0], name[1], type_="check")
    # BEGIN AUTOGEN-DDL-DOWNGRADE
    op.drop_index(
        "ix_transport_outcome_venue_offer",
        table_name="transport_outcome_journal",
        postgresql_where=sa.text("venue_offer_id IS NOT NULL"),
    )
    op.drop_column("submission_attempt_journal", "cell_id")
    op.drop_constraint(
        "uq_ledger_observation_wallet_symbol", "ledger_observation_wallet", type_="unique"
    )
    op.drop_column("ledger_observation_wallet", "symbol")
    op.drop_column("ledger_observation", "trades_requested_end_ms")
    op.drop_column("ledger_observation", "trades_requested_start_ms")
    op.drop_column("ledger_observation", "trades_complete")
    op.drop_index(
        "ix_execution_resolution_venue_offer",
        table_name="execution_resolution_journal",
        postgresql_where=sa.text("venue_offer_id IS NOT NULL"),
    )
    op.drop_column("accepted_capital_basis_symbol", "block")
    op.add_column(
        "accepted_capital_basis",
        sa.Column("policy_revision_id", sa.UUID(), autoincrement=False, nullable=False),
    )
    op.add_column(
        "accepted_capital_basis",
        sa.Column(
            "authorization_block",
            postgresql.JSONB(astext_type=sa.Text()),
            autoincrement=False,
            nullable=True,
        ),
    )
    op.add_column(
        "accepted_capital_basis",
        sa.Column("credit_cells_present", sa.BOOLEAN(), autoincrement=False, nullable=False),
    )
    op.create_foreign_key(
        op.f("accepted_capital_basis_policy_revision_id_fkey"),
        "accepted_capital_basis",
        "capital_policy_revisions",
        ["policy_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_column("accepted_capital_basis", "scope_block")
    op.drop_column("accepted_capital_basis", "attempt_seq_high_water")
    op.drop_table("ledger_observation_trade")
    # END AUTOGEN-DDL-DOWNGRADE
    op.create_check_constraint(
        "ck_ledger_observation_acceptance", "ledger_observation", _ACCEPTANCE_V1
    )

    for table, columns in _DROPPED.items():
        _revoke_columns(table, columns)
        _grant_reader(table, columns)
