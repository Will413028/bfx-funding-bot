"""Separate why trading stopped, and let authorisation re-derive its own amount."""
import sqlalchemy as sa

from alembic import op

revision = "a7f3c1d9e204"
down_revision = "e5c9a3f10b62"
branch_labels = None
depends_on = None

# Identical to b4e6f8a0c203's guard except for the `minimum_amount` clause, which
# gains the prepared -> authorized exemption. Kept whole rather than patched so
# both directions install a complete, readable function.
_GUARD = """CREATE OR REPLACE FUNCTION public.guard_release_session() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF (NEW.id,NEW.exchange_account_id,NEW.deployment_environment,NEW.symbol,NEW.cell,
            NEW.strategy,NEW.max_amount,NEW.expires_at_ms,NEW.created_at_ms)
          IS DISTINCT FROM (OLD.id,OLD.exchange_account_id,OLD.deployment_environment,OLD.symbol,OLD.cell,
            OLD.strategy,OLD.max_amount,OLD.expires_at_ms,OLD.created_at_ms)
          OR (OLD.binding IS NOT NULL AND NEW.binding IS DISTINCT FROM OLD.binding)
          OR (OLD.halt_id IS NOT NULL AND NEW.halt_id IS DISTINCT FROM OLD.halt_id)
          OR (OLD.minimum_amount IS NOT NULL AND NEW.minimum_amount IS DISTINCT FROM OLD.minimum_amount
              {minimum_amount_exemption})
          OR (OLD.authorized_at_ms IS NOT NULL AND (NEW.authorized_at_ms,NEW.authorized_by)
             IS DISTINCT FROM (OLD.authorized_at_ms,OLD.authorized_by))
          OR (OLD.promoted_halt_id IS NOT NULL AND NEW.promoted_halt_id IS DISTINCT FROM OLD.promoted_halt_id)
          OR (OLD.consumed_at_ms IS NOT NULL AND
             (NEW.consumed_at_ms,NEW.permit_id,NEW.decision_id,NEW.attempt_id,NEW.exact_amount)
              IS DISTINCT FROM (OLD.consumed_at_ms,OLD.permit_id,OLD.decision_id,OLD.attempt_id,OLD.exact_amount))
          THEN RAISE EXCEPTION 'immutable release binding'; END IF;
        IF NEW.state <> OLD.state AND NOT (
          (OLD.state='requested' AND NEW.state='prepared') OR
          (OLD.state='prepared' AND NEW.state='authorized') OR
          (OLD.state='authorized' AND NEW.state='consumed') OR
          (OLD.state='consumed' AND NEW.state='observed') OR
          (OLD.state='observed' AND NEW.state='validated') OR
          (OLD.state='validated' AND NEW.state='promoted') OR
          (OLD.state <> 'blocked' AND NEW.state='blocked'))
          THEN RAISE EXCEPTION 'invalid release transition'; END IF;
        RETURN NEW; END $$"""

_EXEMPTION = "AND NOT (OLD.state='prepared' AND NEW.state='authorized')"


def upgrade() -> None:
    # A halt an operator asked for and a halt the system imposed need different
    # exits. Before this column they shared one, so clearing a maintenance pause
    # required the release-promotion path -- a real-money canary submit to undo a
    # database upgrade. Existing rows default to `safety`, the closed posture:
    # nothing is inferred from their free-text reason.
    op.add_column(
        "trading_halt",
        sa.Column("kind", sa.Text(), nullable=False, server_default="safety"),
    )
    op.create_check_constraint(
        "ck_trading_halt_kind",
        "trading_halt",
        "kind IN ('maintenance', 'safety', 'release')",
    )

    # Authorisation re-derives the canary amount against the FX observed at that
    # moment; the prepare-time figure is only the preview the operator saw. The
    # guard froze `minimum_amount` at prepare, so the two protections deadlocked
    # and every authorisation whose FX had moved died with `immutable release
    # binding`. Exempt exactly that transition; every other update stays frozen.
    op.execute(_GUARD.format(minimum_amount_exemption=_EXEMPTION))


def downgrade() -> None:
    op.execute(_GUARD.format(minimum_amount_exemption=""))
    op.drop_constraint("ck_trading_halt_kind", "trading_halt", type_="check")
    op.drop_column("trading_halt", "kind")
