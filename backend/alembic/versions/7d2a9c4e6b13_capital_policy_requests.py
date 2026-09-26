"""Operator enable/disable of one currency's CapitalPolicy, through the outbox.

Revises 5b1e7c9d2a40. Lending envelope ADR 2026-09-25 D4: the per-currency
``enabled`` flag is the everyday stop, so the operator changes it from the UI
(TOTP) the way resume and kill work: the web API inserts a request, the account
daemon re-checks the operator under the account lock and appends a policy
revision through the amendment path.

- ``capital_policy_requests`` follows the outbox contract of 1c435a35dcb4 and
  5b1e7c9d2a40: request columns immutable, ``state`` leaves ``requested``
  exactly once, rows never deleted; one pending request per currency and
  action. A separate table from ``trading_control_requests``: a kill keeps its
  own queue and never waits behind a currency toggle.
- The runtime role could not write policy at all (a9d3e5f7b102); it now may
  INSERT a revision and move a head, and a trigger narrows that to exactly what
  a toggle does: the next revision of an existing head, equal to it except for
  ``enabled``, naming the request it applies. The table owner (the amendment
  script's role) is unaffected.

Grants: the bot reads requests and records outcomes; the web API reads them,
inserts only the request columns, and reads the policy it lists.

Downgrade drops the table and takes back the runtime role's policy writes; a
populated request table is refused (the requests are the only record of who
changed a policy from the UI). Read grants are left as they are.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "7d2a9c4e6b13"
down_revision = "5b1e7c9d2a40"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

ACTIONS = ("enable", "disable")
REQUEST_COLUMNS = ("request_id,exchange_account_id,deployment_environment,symbol,action,"
                   "reason,requested_by,created_at_ms")
WORKER_COLUMNS = "state,processed_at_ms,outcome_reason,policy_revision_id"

# SECURITY INVOKER: it reads the head as the writing role, which holds SELECT.
_RUNTIME_REVISION_GUARD = """CREATE FUNCTION public.guard_runtime_policy_revision()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE prev public.capital_policy_revisions%ROWTYPE;
    BEGIN
      IF pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE') THEN
        RETURN NEW;  -- the owner: the amendment script and migrations
      END IF;
      SELECT r.* INTO prev FROM public.capital_policy_heads h
        JOIN public.capital_policy_revisions r ON r.id = h.revision_id
        WHERE h.exchange_account_id = NEW.exchange_account_id
          AND h.deployment_environment = NEW.deployment_environment AND h.symbol = NEW.symbol;
      IF NOT FOUND OR NEW.revision <> prev.revision + 1
         OR NEW.schema_version <> prev.schema_version
         OR jsonb_typeof(NEW.policy -> 'enabled') IS DISTINCT FROM 'boolean'
         OR (NEW.policy - 'enabled') IS DISTINCT FROM (prev.policy - 'enabled')
         OR coalesce(NEW.source ->> 'request_id', '') = '' THEN
        RAISE EXCEPTION 'runtime policy revision may only toggle enabled of the current revision';
      END IF;
      RETURN NEW;
    END $$"""

_RUNTIME_HEAD_GUARD = """CREATE FUNCTION public.guard_runtime_policy_head()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN
      IF pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE') THEN
        RETURN NEW;
      END IF;
      IF NEW.revision <> OLD.revision + 1 OR NOT EXISTS (
          SELECT FROM public.capital_policy_revisions r
          WHERE r.id = NEW.revision_id AND r.revision = NEW.revision
            AND r.exchange_account_id = OLD.exchange_account_id
            AND r.deployment_environment = OLD.deployment_environment AND r.symbol = OLD.symbol) THEN
        RAISE EXCEPTION 'runtime policy head may only advance to the next revision of its scope';
      END IF;
      RETURN NEW;
    END $$"""


def upgrade() -> None:
    op.create_table(
        "capital_policy_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                                name="fk_capital_policy_requests_account"), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("processed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("policy_revision_id", sa.Uuid(),
                  sa.ForeignKey("capital_policy_revisions.id", ondelete="RESTRICT",
                                name="fk_capital_policy_requests_revision"), nullable=True),
        sa.CheckConstraint("action IN ('enable', 'disable')", name="ck_capital_policy_requests_action"),
        sa.CheckConstraint(
            "length(symbol) BETWEEN 3 AND 16 AND length(trim(reason)) BETWEEN 1 AND 500 "
            "AND length(trim(requested_by)) > 0 AND created_at_ms >= 0",
            name="ck_capital_policy_requests_evidence",
        ),
        sa.CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL "
            "AND policy_revision_id IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL AND policy_revision_id IS NOT NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL AND policy_revision_id IS NULL)",
            name="ck_capital_policy_requests_outcome",
        ),
    )
    op.create_index("uq_capital_policy_requests_pending", "capital_policy_requests",
                    ["exchange_account_id", "deployment_environment", "symbol", "action"],
                    unique=True, postgresql_where=sa.text("state = 'requested'"))
    op.create_index("ix_capital_policy_requests_queue", "capital_policy_requests",
                    ["exchange_account_id", "deployment_environment", "state", "created_at_ms"])
    names = REQUEST_COLUMNS.split(",")
    op.execute(f"""CREATE FUNCTION public.guard_capital_policy_request() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in names)})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in names)})
          THEN RAISE EXCEPTION 'immutable capital policy request'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION 'invalid capital policy request transition'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE FUNCTION public.reject_capital_policy_request_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'immutable capital policy request history'; END $$""")
    op.execute("CREATE TRIGGER capital_policy_request_transition BEFORE UPDATE "
               "ON public.capital_policy_requests FOR EACH ROW "
               "EXECUTE FUNCTION public.guard_capital_policy_request()")
    op.execute("CREATE TRIGGER capital_policy_request_no_delete BEFORE DELETE "
               "ON public.capital_policy_requests FOR EACH ROW "
               "EXECUTE FUNCTION public.reject_capital_policy_request_mutation()")
    op.execute("CREATE TRIGGER capital_policy_request_no_truncate BEFORE TRUNCATE "
               "ON public.capital_policy_requests FOR EACH STATEMENT "
               "EXECUTE FUNCTION public.reject_capital_policy_request_mutation()")

    op.execute(_RUNTIME_REVISION_GUARD)
    op.execute(_RUNTIME_HEAD_GUARD)
    op.execute("CREATE TRIGGER runtime_policy_revision BEFORE INSERT ON public.capital_policy_revisions "
               "FOR EACH ROW EXECUTE FUNCTION public.guard_runtime_policy_revision()")
    op.execute("CREATE TRIGGER runtime_policy_head BEFORE UPDATE ON public.capital_policy_heads "
               "FOR EACH ROW EXECUTE FUNCTION public.guard_runtime_policy_head()")

    op.execute("REVOKE ALL ON FUNCTION public.guard_capital_policy_request(), "
               "public.reject_capital_policy_request_mutation(), "
               "public.guard_runtime_policy_revision(), public.guard_runtime_policy_head() FROM PUBLIC")
    op.execute("REVOKE ALL ON public.capital_policy_requests FROM PUBLIC")
    op.execute(f"""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE ALL ON public.capital_policy_requests FROM bfx_bot;
        GRANT SELECT ON public.capital_policy_requests TO bfx_bot;
        GRANT UPDATE ({WORKER_COLUMNS}) ON public.capital_policy_requests TO bfx_bot;
        GRANT SELECT, INSERT ON public.capital_policy_revisions TO bfx_bot;
        GRANT SELECT ON public.capital_policy_heads TO bfx_bot;
        GRANT UPDATE (revision_id, revision) ON public.capital_policy_heads TO bfx_bot;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webapi') THEN
        REVOKE ALL ON public.capital_policy_requests FROM bfx_webapi;
        GRANT SELECT ON public.capital_policy_requests TO bfx_webapi;
        GRANT INSERT ({REQUEST_COLUMNS}) ON public.capital_policy_requests TO bfx_webapi;
        GRANT SELECT ON public.capital_policy_heads, public.capital_policy_revisions TO bfx_webapi;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') THEN
        REVOKE ALL ON public.capital_policy_requests FROM bfx_webauth;
      END IF; END $$""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT FROM public.capital_policy_requests) THEN
          RAISE EXCEPTION 'refuse downgrade of recorded capital policy requests';
        END IF; END $$""")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_bot') THEN
        REVOKE INSERT ON public.capital_policy_revisions FROM bfx_bot;
        REVOKE UPDATE (revision_id, revision) ON public.capital_policy_heads FROM bfx_bot;
      END IF; END $$""")
    op.execute("DROP TRIGGER runtime_policy_head ON public.capital_policy_heads")
    op.execute("DROP TRIGGER runtime_policy_revision ON public.capital_policy_revisions")
    op.execute("DROP FUNCTION public.guard_runtime_policy_head()")
    op.execute("DROP FUNCTION public.guard_runtime_policy_revision()")
    op.drop_table("capital_policy_requests")
    op.execute("DROP FUNCTION public.guard_capital_policy_request()")
    op.execute("DROP FUNCTION public.reject_capital_policy_request_mutation()")
