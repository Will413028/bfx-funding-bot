"""Record which capital authority the runtime runs under, and hold the ledger dormant.

``capital_authority_epoch`` is insert-only and seeded ``legacy``; the latest row
by ``epoch_seq`` is the authority. Only the owner appends (the S1-7 switch), and
every build reads it once at boot and refuses a value it does not support.

Until the latest epoch is ``ledger``, a role other than the table owner cannot
write any ledger fact table: a statement trigger rejects its INSERT (and its
UPDATE on the clock and the mirrors, the only ledger tables runtime may update).
Owner writes -- migrations, tests, the seed during the halt -- pass.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_ROLES = ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader")
_EPOCH = "capital_authority_epoch"
_EPOCH_COLUMNS = ("epoch_seq", "authority", "set_at_ms", "actor", "reason", "evidence")
_EPOCH_READERS = ("bfx_bot", "bfx_webapi")
_FUNCTION = "guard_ledger_authority"
_TRIGGER = "ledger_authority_write"
# Every ledger fact table runtime (bfx_bot) may write at e5f6a7b8c9d0.
_INSERT_ONLY = (
    "ledger_observation_query",
    "ledger_observation",
    "ledger_observation_wallet",
    "ledger_observation_offer",
    "ledger_observation_credit",
    "ledger_observation_offer_history",
    "ledger_observation_credit_history",
    "ledger_observation_trade",
    "submission_attempt_journal",
    "transport_outcome_journal",
    "quarantine_opening",
    "quarantine_member",
    "execution_resolution_journal",
    "accepted_capital_basis",
    "accepted_capital_basis_symbol",
    "accepted_capital_basis_cell",
    "accepted_capital_basis_credit",
    "accepted_capital_basis_credit_cell",
    "accepted_capital_basis_attempt",
    "accepted_capital_basis_quarantine",
)
# bfx_bot may also UPDATE these (the clock revision, the mirror rows).
_UPDATABLE = ("capital_command_clock", "venue_offer_mirror", "venue_credit_mirror")
GUARDED = _INSERT_ONLY + _UPDATABLE


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    # BEGIN AUTOGEN-DDL-UPGRADE
    op.create_table(
        "capital_authority_epoch",
        sa.Column("epoch_seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("authority", sa.Text(), nullable=False),
        sa.Column("set_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "authority IN ('legacy', 'ledger')", name="ck_capital_authority_epoch_authority"
        ),
        sa.PrimaryKeyConstraint("epoch_seq"),
    )
    # END AUTOGEN-DDL-UPGRADE

    op.execute(
        f"INSERT INTO public.{_EPOCH} (epoch_seq, authority, set_at_ms, actor, reason, evidence) "
        "VALUES (1, 'legacy', (extract(epoch FROM clock_timestamp()) * 1000)::bigint, "
        f"'migration {revision}', 'initial authority', NULL)"
    )
    op.execute(
        f"CREATE TRIGGER immutable_ledger_write BEFORE UPDATE OR DELETE ON public.{_EPOCH} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_ledger_mutation()"
    )
    op.execute(
        f"CREATE TRIGGER immutable_ledger_truncate BEFORE TRUNCATE ON public.{_EPOCH} "
        "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_ledger_mutation()"
    )

    # Not SECURITY DEFINER: current_user is the writing role, so the epoch read
    # needs that role's SELECT (granted below to bfx_bot).
    op.execute(f"""CREATE FUNCTION public.{_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF current_user IS DISTINCT FROM
               (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID)
             AND (SELECT e.authority FROM public.{_EPOCH} e
                    ORDER BY e.epoch_seq DESC LIMIT 1) IS DISTINCT FROM 'ledger' THEN
            RAISE EXCEPTION 'ledger write requires ledger authority: %', TG_TABLE_NAME;
          END IF;
          RETURN NULL;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_FUNCTION}() FROM PUBLIC")
    for name in _INSERT_ONLY:
        op.execute(
            f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT ON public.{name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION public.{_FUNCTION}()"
        )
    for name in _UPDATABLE:
        op.execute(
            f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT OR UPDATE ON public.{name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION public.{_FUNCTION}()"
        )

    listed = ", ".join(_EPOCH_COLUMNS)
    op.execute(f"REVOKE ALL ON TABLE public.{_EPOCH} FROM PUBLIC")
    op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{_EPOCH} FROM PUBLIC")
    for role in _ROLES:
        if _role_exists(role):
            op.execute(f"REVOKE ALL ON TABLE public.{_EPOCH} FROM {role}")
            op.execute(f"REVOKE ALL ({listed}) ON TABLE public.{_EPOCH} FROM {role}")
    for role in _EPOCH_READERS:
        if _role_exists(role):
            op.execute(f"GRANT SELECT ON public.{_EPOCH} TO {role}")
    # The reader group holds column grants only (fresh-host-setup's allowlist).
    if _role_exists("bfx_cutover_reader"):
        op.execute(f"GRANT SELECT ({listed}) ON public.{_EPOCH} TO bfx_cutover_reader")


def downgrade() -> None:
    # A later epoch means an authority switch happened; the older build must not
    # silently lose that record.
    if op.get_bind().execute(text(f"SELECT count(*) FROM public.{_EPOCH}")).scalar() != 1:
        raise RuntimeError(f"refuse downgrade of switched authority: {_EPOCH}")
    for name in GUARDED:
        op.execute(f"DROP TRIGGER {_TRIGGER} ON public.{name}")
    op.execute(f"DROP FUNCTION public.{_FUNCTION}()")
    # BEGIN AUTOGEN-DDL-DOWNGRADE
    op.drop_table("capital_authority_epoch")
    # END AUTOGEN-DDL-DOWNGRADE
