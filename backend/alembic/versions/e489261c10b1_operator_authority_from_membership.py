"""Operator authority comes from membership alone; nothing in the database reads ``auth``.

Quality plan 9d-1 (security/9d-tailscale-ingress-brief.md §4): the expand step before the
operator entry moves to Tailscale and the ``auth`` schema is dropped (9d-7).

- ``public.operator_authorized(account, actor)``: an owner/operator member of a live
  account. Same signature, ``SECURITY DEFINER``, ``search_path`` and grants, so the policy
  trigger that calls it (``guard_runtime_policy_revision``) is unchanged. It no longer joins
  ``auth."user"`` (admin, not banned, TOTP enrolled): the operator's identity becomes the
  tailnet's, and a membership row is writable only by the table owner.
- ``release_archive.release_operator_authorized``: ``false``. Its tables are frozen
  (``archive_frozen``), so nothing reaches it; it only must not read ``auth``.
- ``bfx_bot`` keeps SELECT only on ``exchange_account_memberships`` and
  ``exchange_accounts``. The default privileges of fresh-host-setup §1a gave it every
  write on both, which did not matter while ``auth`` decided authority; with membership
  the only authority, a runtime role that could write it could authorize anyone. Nothing
  in the bot writes either table (accounts and memberships come from owner scripts).

Asserted in the same transaction, under SHARE locks on the two tables the verdict reads
(docs/adr/2026-10-08-migrations-assert-their-data-and-deploy-rehearses-them.md):

1. the new body authorizes exactly the (account, user) pairs the old one did, so today's
   operator keeps the daemon's authority and no one gains it;
2. at most one distinct user is authorized: the daemon's Python gate (the configured
   operator id) goes away with this revision, and a second member would otherwise gain
   the daemon's authority;
3. no function body in the database reads ``auth``;
4. ``bfx_bot`` holds no write on either table, at table or column level.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md); a later revision restores
the old body if needed, ``auth`` stays until 9d-7.

Revision ID: e489261c10b1
Revises: 36e59526f8cf
"""
from sqlalchemy import text

from alembic import op

revision = "e489261c10b1"
down_revision = "36e59526f8cf"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

MEMBERSHIPS = "public.exchange_account_memberships"
ACCOUNTS = "public.exchange_accounts"
AUTHORIZED = "bfx_9d1_authorized"
WRITES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def _assert(sql: str, message: str) -> None:
    if not op.get_bind().execute(text(sql)).scalar():
        raise RuntimeError(message)


def _snapshot(table: str) -> None:
    op.execute(f"CREATE TEMP TABLE {table} ON COMMIT DROP AS "
               f"SELECT m.exchange_account_id, m.user_id FROM {MEMBERSHIPS} m "
               "WHERE public.operator_authorized(m.exchange_account_id, m.user_id)")


def upgrade() -> None:
    op.execute(f"LOCK TABLE {MEMBERSHIPS}, {ACCOUNTS} IN SHARE MODE")
    _snapshot(f"{AUTHORIZED}_old")

    op.execute('''CREATE OR REPLACE FUNCTION public.operator_authorized(account uuid, actor text)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT EXISTS (SELECT 1 FROM public.exchange_account_memberships m
          JOIN public.exchange_accounts a ON a.id=m.exchange_account_id
          WHERE a.id=account AND m.user_id=actor AND m.role IN ('owner','operator')
            AND a.lifecycle_status IN ('active','halted') FOR SHARE OF m,a)
        $$''')
    op.execute('''CREATE OR REPLACE FUNCTION release_archive.release_operator_authorized(
            account uuid, actor text)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $$
        SELECT false
        $$''')

    _snapshot(f"{AUTHORIZED}_new")
    _assert(f"SELECT NOT EXISTS (SELECT * FROM {AUTHORIZED}_old EXCEPT "
            f"SELECT * FROM {AUTHORIZED}_new) AND NOT EXISTS (SELECT * FROM {AUTHORIZED}_new "
            f"EXCEPT SELECT * FROM {AUTHORIZED}_old)",
            "membership-only authority differs from the old verdict: a member who is not a "
            "TOTP-enrolled, unbanned admin would gain it, or the operator would lose it")
    _assert(f"SELECT count(DISTINCT user_id) <= 1 FROM {AUTHORIZED}_new",
            "more than one user is an owner/operator member of a live account: removing the "
            "configured-operator gate would give the daemon's authority to each of them")
    # Any spelling: auth."user", auth.user, auth . "user", or another auth table.
    _assert(r"""SELECT count(*) = 0 FROM pg_proc p
                 JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname <> 'auth' AND p.prosrc ~* '\mauth\s*\.'""",
            "a function body outside auth still reads the auth schema")

    if _role_exists("bfx_bot"):
        for table in (MEMBERSHIPS, ACCOUNTS):
            op.execute(f"REVOKE ALL ON {table} FROM bfx_bot")
            op.execute(f"GRANT SELECT ON {table} TO bfx_bot")
            for privilege in WRITES:
                _assert(f"SELECT NOT has_table_privilege('bfx_bot', '{table}', '{privilege}')",
                        f"bfx_bot still holds {privilege} on {table}")
            for privilege in ("INSERT", "UPDATE", "REFERENCES"):
                _assert(f"SELECT NOT has_any_column_privilege('bfx_bot', '{table}', "
                        f"'{privilege}')", f"bfx_bot still holds column {privilege} on {table}")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
