"""Extend quarantine membership and accepted capital credit evidence.

Revision ID: c3d4e5f6a7b8
Revises: b1e2d3a4c5f6
"""

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b1e2d3a4c5f6"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_TABLE_COLUMNS = {
    "quarantine_member": (
        "quarantine_id",
        "source_kind",
        "venue_object_id",
        "observation_id",
        "amount_at_join",
    ),
    "accepted_capital_basis_credit": (
        "basis_id",
        "source_kind",
        "venue_credit_id",
        "symbol",
        "amount",
        "period_days",
        "mts_opening",
        "attribution_basis",
    ),
    "accepted_capital_basis_credit_cell": (
        "basis_id",
        "source_kind",
        "venue_credit_id",
        "cell_id",
    ),
}
_NEW_TABLES = ("accepted_capital_basis_credit", "accepted_capital_basis_credit_cell")
_READER_COLUMNS = {
    "quarantine_member": (
        "quarantine_id",
        "source_kind",
        "venue_object_id",
        "observation_id",
        "amount_at_join",
    ),
    "accepted_capital_basis_credit": (
        "basis_id",
        "source_kind",
        "venue_credit_id",
        "symbol",
        "amount",
        "period_days",
        "mts_opening",
        "attribution_basis",
    ),
    "accepted_capital_basis_credit_cell": (
        "basis_id",
        "source_kind",
        "venue_credit_id",
        "cell_id",
    ),
}


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _revoke_defaults(role: str) -> None:
    for name, columns in _TABLE_COLUMNS.items():
        op.execute(f"REVOKE ALL ON TABLE public.{name} FROM {role}")
        op.execute(f"REVOKE ALL ({', '.join(columns)}) ON TABLE public.{name} FROM {role}")


def upgrade() -> None:
    # BEGIN AUTOGEN-DDL-UPGRADE
    op.create_table(
        "accepted_capital_basis_credit",
        sa.Column("basis_id", sa.UUID(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("venue_credit_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=True),
        sa.Column("mts_opening", sa.BigInteger(), nullable=True),
        sa.Column("attribution_basis", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "attribution_basis IN ('trade','carry','recent_fill','unattributed')",
            name="ck_accepted_basis_credit_attribution",
        ),
        sa.CheckConstraint(
            "source_kind IN ('credit','loan')", name="ck_accepted_basis_credit_kind"
        ),
        sa.CheckConstraint("amount >= 0", name="ck_accepted_basis_credit_amount"),
        sa.ForeignKeyConstraint(["basis_id"], ["accepted_capital_basis.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("basis_id", "source_kind", "venue_credit_id"),
    )
    op.create_index(
        "ix_accepted_basis_credit_cell_lookup",
        "accepted_capital_basis_credit",
        ["basis_id", "symbol", "period_days", "mts_opening"],
        unique=False,
    )
    op.create_table(
        "accepted_capital_basis_credit_cell",
        sa.Column("basis_id", sa.UUID(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("venue_credit_id", sa.Text(), nullable=False),
        sa.Column("cell_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["basis_id", "source_kind", "venue_credit_id"],
            [
                "accepted_capital_basis_credit.basis_id",
                "accepted_capital_basis_credit.source_kind",
                "accepted_capital_basis_credit.venue_credit_id",
            ],
            name="fk_accepted_basis_credit_cell_credit",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("basis_id", "source_kind", "venue_credit_id", "cell_id"),
    )
    op.create_index(
        "ix_funding_trades_scope_symbol_created",
        "funding_trades",
        ["exchange_account_id", "deployment_environment", "symbol", "mts_create"],
        unique=False,
    )
    op.add_column("quarantine_member", sa.Column("source_kind", sa.Text(), nullable=False))
    op.add_column("quarantine_member", sa.Column("venue_object_id", sa.Text(), nullable=False))
    op.drop_column("quarantine_member", "venue_offer_id")
    # Autogenerate does not emit primary-key or CHECK changes. Dropping
    # venue_offer_id above dropped quarantine_member_pkey with it.
    op.create_primary_key(
        "quarantine_member_pkey",
        "quarantine_member",
        ["quarantine_id", "source_kind", "venue_object_id"],
    )
    op.create_check_constraint(
        "ck_quarantine_member_kind",
        "quarantine_member",
        "source_kind IN ('offer','credit','loan')",
    )
    # END AUTOGEN-DDL-UPGRADE

    op.execute("""CREATE OR REPLACE FUNCTION public.guard_ledger_scope() RETURNS trigger
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
        END $$""")

    op.execute("REVOKE ALL ON FUNCTION public.guard_ledger_scope() FROM PUBLIC")
    for name in _NEW_TABLES:
        op.execute(
            f"CREATE TRIGGER immutable_ledger_write BEFORE UPDATE OR DELETE ON public.{name} "
            "FOR EACH ROW EXECUTE FUNCTION public.reject_ledger_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER immutable_ledger_truncate BEFORE TRUNCATE ON public.{name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_ledger_mutation()"
        )
    for name, columns in _TABLE_COLUMNS.items():
        op.execute(f"REVOKE ALL ON TABLE public.{name} FROM PUBLIC")
        op.execute(f"REVOKE ALL ({', '.join(columns)}) ON TABLE public.{name} FROM PUBLIC")
    for role in ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader"):
        if _role_exists(role):
            _revoke_defaults(role)
    if _role_exists("bfx_bot"):
        for name in _NEW_TABLES:
            op.execute(f"GRANT SELECT, INSERT ON public.{name} TO bfx_bot")
        op.execute("GRANT SELECT, INSERT ON public.quarantine_member TO bfx_bot")
    if _role_exists("bfx_cutover_reader"):
        for name, columns in _READER_COLUMNS.items():
            op.execute(
                f"GRANT SELECT ({', '.join(columns)}) ON public.{name} TO bfx_cutover_reader"
            )


def downgrade() -> None:
    for name in (*_NEW_TABLES, "quarantine_member"):
        if op.get_bind().execute(text(f"SELECT EXISTS (SELECT 1 FROM public.{name})")).scalar():
            raise RuntimeError("refuse downgrade of populated ledger: " + name)
    # BEGIN AUTOGEN-DDL-DOWNGRADE
    op.add_column(
        "quarantine_member",
        sa.Column("venue_offer_id", sa.TEXT(), autoincrement=False, nullable=False),
    )
    op.drop_column("quarantine_member", "venue_object_id")
    op.drop_column("quarantine_member", "source_kind")
    op.drop_index("ix_funding_trades_scope_symbol_created", table_name="funding_trades")
    op.drop_table("accepted_capital_basis_credit_cell")
    op.drop_index(
        "ix_accepted_basis_credit_cell_lookup", table_name="accepted_capital_basis_credit"
    )
    op.drop_table("accepted_capital_basis_credit")
    # Dropping source_kind/venue_object_id above dropped the new key and CHECK.
    op.create_primary_key(
        "quarantine_member_pkey", "quarantine_member", ["quarantine_id", "venue_offer_id"]
    )
    # END AUTOGEN-DDL-DOWNGRADE

    op.execute("""CREATE OR REPLACE FUNCTION public.guard_ledger_scope() RETURNS trigger
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
                SELECT observation_id, venue_offer_id, symbol FROM public.ledger_observation_offer
                UNION ALL
                SELECT observation_id, venue_offer_id, symbol
                  FROM public.ledger_observation_offer_history
              ) h ON h.observation_id = o.id
              WHERE o.id = NEW.observation_id AND h.venue_offer_id = NEW.venue_offer_id
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
        END $$""")

    op.execute("REVOKE ALL ON FUNCTION public.guard_ledger_scope() FROM PUBLIC")
    if _role_exists("bfx_bot"):
        op.execute("GRANT SELECT, INSERT ON public.quarantine_member TO bfx_bot")
    if _role_exists("bfx_cutover_reader"):
        op.execute(
            "GRANT SELECT (quarantine_id, venue_offer_id, observation_id, amount_at_join) "
            "ON public.quarantine_member TO bfx_cutover_reader"
        )
