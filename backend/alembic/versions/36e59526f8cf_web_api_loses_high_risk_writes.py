"""The web API loses its high-risk writes; an active credential always names its Bitfinex user.

ADR 2026-10-15 D4 and its amendment: resume, enabling a currency, adjudicating an
uncertainty and writing a credential go through the operator CLI only; the web API keeps
kill, disabling a currency and its reads.

- ``exchange_account_credentials``: a CHECK that an active row has ``bitfinex_user_id``
  (the rotation worker compares a new key's account with it; the TOFU fill and the live
  lookup of a NULL are gone). Asserted of the existing rows first, under a lock that keeps a
  previous web API from promoting a NULL row in between
  (docs/adr/2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md).
- ``bfx_webapi`` loses INSERT and UPDATE on the credentials and INSERT on
  ``uncertainty_resolution_requests`` (every row there is an adjudication). A table-level
  REVOKE takes the column-level grants with it. Its SELECTs and the kill / disable INSERT
  columns are unchanged.
- ``trading_control_requests`` ('resume') and ``capital_policy_requests`` ('enable'): a
  trigger lets only the table owner (migrations, owner scripts) and ``bfx_operator`` write
  that action. An allowlist, not "refuse bfx_webapi": any other role holding the request
  columns' INSERT is refused too. ``bfx_bot`` has no INSERT on either table.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: 36e59526f8cf
Revises: 1d34a3306e02
"""
from sqlalchemy import text

from alembic import op

revision = "36e59526f8cf"
down_revision = "1d34a3306e02"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

CREDENTIALS = "exchange_account_credentials"
ADJUDICATIONS = "uncertainty_resolution_requests"
CHECK = "ck_exchange_account_credentials_active_user_id"
GUARD = "public.guard_operator_only_request_action"
# (table, trigger, the action only the owner and the operator CLI may write)
OPERATOR_ONLY = (
    ("trading_control_requests", "trading_control_request_operator_only", "resume"),
    ("capital_policy_requests", "capital_policy_request_operator_only", "enable"),
)


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _assert(sql: str, message: str) -> None:
    if not op.get_bind().execute(text(sql)).scalar():
        raise RuntimeError(message)


def upgrade() -> None:
    op.execute(f"LOCK TABLE public.{CREDENTIALS} IN ACCESS EXCLUSIVE MODE")
    _assert(f"SELECT count(*) = 0 FROM public.{CREDENTIALS} "
            "WHERE lifecycle_status = 'active' AND bitfinex_user_id IS NULL",
            "active credential without bitfinex_user_id: run the bot once on a working key to "
            "fill it, or rotate")
    op.create_check_constraint(CHECK, CREDENTIALS,
                               "lifecycle_status <> 'active' OR bitfinex_user_id IS NOT NULL")

    webapi = _role_exists("bfx_webapi")
    if webapi:
        op.execute(f"REVOKE INSERT, UPDATE ON public.{CREDENTIALS} FROM bfx_webapi")
        op.execute(f"REVOKE INSERT ON public.{ADJUDICATIONS} FROM bfx_webapi")

    # SECURITY INVOKER: current_user is the role writing the row. The owner test is
    # guard_runtime_policy_revision's (5e820d6dc7da).
    op.execute(f"""CREATE FUNCTION {GUARD}() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        IF NEW.action = TG_ARGV[0] AND NOT (
             pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID), 'USAGE')
             OR current_user = 'bfx_operator')
          THEN RAISE EXCEPTION '% may be requested only through the operator CLI', NEW.action
               USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN NEW; END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION {GUARD}() FROM PUBLIC")
    for table, trigger, action in OPERATOR_ONLY:
        op.execute(f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE OF action "
                   f"ON public.{table} FOR EACH ROW EXECUTE FUNCTION {GUARD}('{action}')")

    if webapi:
        for table, privilege in ((CREDENTIALS, "INSERT"), (CREDENTIALS, "UPDATE"),
                                 (ADJUDICATIONS, "INSERT")):
            _assert(f"SELECT NOT has_any_column_privilege('bfx_webapi', 'public.{table}', "
                    f"'{privilege}')", f"bfx_webapi still holds {privilege} on {table}")
    for table, trigger, _action in OPERATOR_ONLY:
        _assert(f"SELECT count(*) = 1 FROM pg_trigger WHERE tgname = '{trigger}' "
                f"AND tgrelid = 'public.{table}'::regclass", f"trigger {trigger} missing")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
