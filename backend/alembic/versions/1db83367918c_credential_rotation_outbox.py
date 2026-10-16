"""Operator rotation of the Bitfinex credential, through the outbox.

The operator CLI seals a new key to the public key the running bot published for this
boot (``credential_rotation_recipients``) and queues a request
(``credential_rotation_requests``); the account daemon verifies the key outside the
account lock, then retires the replaced credential and inserts the new active one in
the transaction that records the outcome, and restarts on it
(``modules/execution/credential_rotation.py``).

- ``credential_rotation_requests`` follows the outbox contract of 1c435a35dcb4 and
  7d2a9c4e6b13: request columns immutable, ``state`` leaves ``requested`` exactly once,
  rows never deleted; one pending request per account and environment.
- ``exchange_account_credentials.operator_request_id`` (nullable, existing rows NULL): the
  credential an applied rotation made active names its request, as every outbox's effect
  row does. The web API's table-level grants on the credentials cover the new column.
- ``credential_rotation_recipients``: one row per bot run that published a key; no
  runtime role may change or delete one, the owner included (a trigger).
- Both carry the realm guard like every ``deployment_environment`` table.

Grants: both tables are first taken away from PUBLIC and every runtime role (production's
default privileges hand new tables to ``bfx_bot``; the test builds hand them to every
role). Then ``bfx_bot`` reads requests and records outcomes, reads and inserts
recipients; its existing grants on the credentials, accounts, ``bot_runs`` and
``deployments`` are what the worker needs. ``bfx_operator`` inserts the request columns
and reads every request column but ``sealed_credential``, reads the recipients, and reads
the credential and run columns the CLI's checks name -- never a credential's key or
ciphertext. ``bfx_webapi`` and ``bfx_webauth`` get nothing.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: 1db83367918c
Revises: 4ab02765a34b
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "1db83367918c"
down_revision = "4ab02765a34b"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

REQUESTS = "credential_rotation_requests"
RECIPIENTS = "credential_rotation_recipients"
REQUEST_COLUMNS = ("request_id,exchange_account_id,deployment_environment,recipient_id,"
                   "replaces_credential_id,sealed_credential,label,reason,requested_by,"
                   "created_at_ms")
WORKER_COLUMNS = "state,processed_at_ms,outcome_reason"
# Every table with a ``deployment_environment`` column this revision adds; the realm coverage
# test adds them to ``a7c3e9f1b2d4``'s list.
REALM_TABLES = (f"public.{RECIPIENTS}", f"public.{REQUESTS}")
_NO_ACCESS = ("bfx_webapi", "bfx_webauth", "bfx_cutover_reader")

# bfx_operator: what the CLI's rotation command and its reads name (operator_commands.py).
OPERATOR_REQUEST_SELECT = tuple(
    c for c in (REQUEST_COLUMNS + "," + WORKER_COLUMNS).split(",") if c != "sealed_credential")
OPERATOR_CREDENTIAL_SELECT = ("id", "exchange_account_id", "venue", "label", "lifecycle_status",
                              "verified_at", "created_at", "operator_request_id")
OPERATOR_RUN_SELECT = ("run_id", "exchange_account_id", "deployment_environment",
                       "started_at_ms", "end_recorded_at_ms")


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    op.create_table(
        RECIPIENTS,
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                                name="fk_credential_rotation_recipients_account"), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("bot_runs.run_id", ondelete="RESTRICT",
                                name="fk_credential_rotation_recipients_run"), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("octet_length(public_key) = 32 AND created_at_ms >= 0",
                           name="ck_credential_rotation_recipients_key"),
    )
    op.create_index("ix_credential_rotation_recipients_run", RECIPIENTS,
                    ["exchange_account_id", "deployment_environment", "run_id", "created_at_ms"])
    op.create_table(
        REQUESTS,
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exchange_accounts.id", ondelete="RESTRICT",
                                name="fk_credential_rotation_requests_account"), nullable=False),
        sa.Column("deployment_environment", sa.Text(), nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey(f"{RECIPIENTS}.recipient_id", ondelete="RESTRICT",
                                name="fk_credential_rotation_requests_recipient"), nullable=False),
        sa.Column("replaces_credential_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("exchange_account_credentials.id", ondelete="RESTRICT",
                                name="fk_credential_rotation_requests_replaces"), nullable=False),
        sa.Column("sealed_credential", sa.LargeBinary(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("processed_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.CheckConstraint("octet_length(sealed_credential) BETWEEN 64 AND 1024",
                           name="ck_credential_rotation_requests_sealed"),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500 AND length(label) <= 64 "
            "AND length(trim(requested_by)) > 0 AND created_at_ms >= 0",
            name="ck_credential_rotation_requests_evidence",
        ),
        sa.CheckConstraint(
            "(state = 'requested' AND processed_at_ms IS NULL AND outcome_reason IS NULL) OR "
            "(state = 'applied' AND processed_at_ms IS NOT NULL) OR "
            "(state IN ('rejected', 'failed') AND processed_at_ms IS NOT NULL "
            "AND outcome_reason IS NOT NULL)",
            name="ck_credential_rotation_requests_outcome",
        ),
    )
    op.create_index("uq_credential_rotation_requests_pending", REQUESTS,
                    ["exchange_account_id", "deployment_environment"],
                    unique=True, postgresql_where=sa.text("state = 'requested'"))
    op.create_index("ix_credential_rotation_requests_queue", REQUESTS,
                    ["exchange_account_id", "deployment_environment", "state", "created_at_ms"])
    # The effect names the request, as in every outbox: the credential a rotation made active.
    # Existing rows stay NULL.
    op.add_column("exchange_account_credentials",
                  sa.Column("operator_request_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_exchange_account_credentials_operator_request",
                          "exchange_account_credentials", REQUESTS,
                          ["operator_request_id"], ["request_id"], ondelete="RESTRICT")

    names = REQUEST_COLUMNS.split(",")
    op.execute(f"""CREATE FUNCTION public.guard_credential_rotation_request() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF ({','.join('NEW.' + c for c in names)})
          IS DISTINCT FROM ({','.join('OLD.' + c for c in names)})
          THEN RAISE EXCEPTION 'immutable credential rotation request'; END IF;
        IF OLD.state <> 'requested' OR NEW.state = 'requested'
          THEN RAISE EXCEPTION 'invalid credential rotation request transition'; END IF;
        RETURN NEW; END $$""")
    op.execute("""CREATE FUNCTION public.reject_credential_rotation_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'immutable credential rotation history'; END $$""")
    op.execute(f"CREATE TRIGGER credential_rotation_request_transition BEFORE UPDATE "
               f"ON public.{REQUESTS} FOR EACH ROW "
               "EXECUTE FUNCTION public.guard_credential_rotation_request()")
    for table, prefix in ((REQUESTS, "credential_rotation_request"),
                          (RECIPIENTS, "credential_rotation_recipient")):
        events = "DELETE" if table == REQUESTS else "UPDATE OR DELETE"
        op.execute(f"CREATE TRIGGER {prefix}_no_change BEFORE {events} "
                   f"ON public.{table} FOR EACH ROW "
                   "EXECUTE FUNCTION public.reject_credential_rotation_mutation()")
        op.execute(f"CREATE TRIGGER {prefix}_no_truncate BEFORE TRUNCATE "
                   f"ON public.{table} FOR EACH STATEMENT "
                   "EXECUTE FUNCTION public.reject_credential_rotation_mutation()")
    for name in REALM_TABLES:
        op.execute(
            f"CREATE TRIGGER database_realm_write BEFORE INSERT OR UPDATE OF "
            f"deployment_environment ON {name} FOR EACH ROW "
            "EXECUTE FUNCTION public.guard_database_realm()"
        )
    op.execute("REVOKE ALL ON FUNCTION public.guard_credential_rotation_request(), "
               "public.reject_credential_rotation_mutation() FROM PUBLIC")

    for table in (REQUESTS, RECIPIENTS):
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC")
        for role in (*_NO_ACCESS, "bfx_bot", "bfx_operator"):
            if _role_exists(role):
                op.execute(f"REVOKE ALL ON public.{table} FROM {role}")
    if _role_exists("bfx_bot"):
        op.execute(f"GRANT SELECT ON public.{REQUESTS} TO bfx_bot")
        op.execute(f"GRANT UPDATE ({WORKER_COLUMNS}) ON public.{REQUESTS} TO bfx_bot")
        op.execute(f"GRANT SELECT, INSERT ON public.{RECIPIENTS} TO bfx_bot")
    if _role_exists("bfx_operator"):
        op.execute(f"GRANT INSERT ({REQUEST_COLUMNS}) ON public.{REQUESTS} TO bfx_operator")
        op.execute(f"GRANT SELECT ({', '.join(OPERATOR_REQUEST_SELECT)}) "
                   f"ON public.{REQUESTS} TO bfx_operator")
        op.execute(f"GRANT SELECT ON public.{RECIPIENTS} TO bfx_operator")
        op.execute(f"GRANT SELECT ({', '.join(OPERATOR_CREDENTIAL_SELECT)}) "
                   "ON public.exchange_account_credentials TO bfx_operator")
        op.execute(f"GRANT SELECT ({', '.join(OPERATOR_RUN_SELECT)}) "
                   "ON public.bot_runs TO bfx_operator")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
