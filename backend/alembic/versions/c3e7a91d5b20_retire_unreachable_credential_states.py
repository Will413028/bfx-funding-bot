"""Retire the credential states nothing writes: ``pending``, ``revoked`` and ``last_verify_error``.

The two writers left (the operator bootstrap and the applied rotation, both in
``credentials.vault``) insert every key already verified and active, naming its Bitfinex user;
a rotation only retires the row it replaces. So a row is ``active`` or ``retired``, always has
``verified_at`` and ``bitfinex_user_id``, and never has a verification error. Production holds
one active row with both set (counted on 2026-10-25). The schema now says so:

* ``lifecycle_status`` allows ``active`` and ``retired`` and has no default (each writer names it);
* ``verified_at`` and ``bitfinex_user_id`` are NOT NULL; the conditional CHECKs that required
  them on active rows go, and the id stays positive;
* ``last_verify_error`` is dropped;
* a Bitfinex key is stored once (``UNIQUE (venue, api_key)``): the rotation's reuse check
  could not be a constraint while a key could sit in several pending rows; now it is, across
  accounts and writers, under no lock of its own.

Refusals, each leaving the table and the revision as they were:

* a row that is not ``active``/``retired``, has a verification error, or lacks ``verified_at``
  or ``bitfinex_user_id``: a database holding one is not this one;
* a key stored more than once;
* a function body that names ``last_verify_error``: ``DROP COLUMN`` does not see those. A view
  on the column refuses on its own: the drop is RESTRICT.

The table is locked for the whole revision; bfx-deploy stops the webapi and the bot while
migrations run, and the table holds a row per key, so the constraint and NOT NULL scans are
instant and nothing waits on the lock.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: c3e7a91d5b20
Revises: f486e11435c9
"""

from sqlalchemy import text

from alembic import op

revision = "c3e7a91d5b20"
down_revision = "f486e11435c9"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

TABLE = "public.exchange_account_credentials"

_ROWS_OUTSIDE_THE_NEW_SHAPE = f"""
SELECT count(*) FROM {TABLE}
WHERE lifecycle_status NOT IN ('active', 'retired')
   OR last_verify_error IS NOT NULL
   OR verified_at IS NULL
   OR bitfinex_user_id IS NULL
"""

_REUSED_KEYS = f"""
SELECT count(*) FROM (SELECT 1 FROM {TABLE} GROUP BY venue, api_key HAVING count(*) > 1) reused
"""

_FUNCTIONS_NAMING_THE_COLUMN = r"""
SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND p.prosrc ~* '(\m|")last_verify_error(\M|")'
"""


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    rows = int(bind.execute(text(_ROWS_OUTSIDE_THE_NEW_SHAPE)).scalar() or 0)
    if rows:
        raise RuntimeError(f"refuse: {rows} credential rows outside active/retired and verified")
    reused = int(bind.execute(text(_REUSED_KEYS)).scalar() or 0)
    if reused:
        raise RuntimeError(f"refuse: {reused} keys stored more than once")
    functions = int(bind.execute(text(_FUNCTIONS_NAMING_THE_COLUMN)).scalar() or 0)
    if functions:
        raise RuntimeError(f"refuse: {functions} function bodies name last_verify_error")
    # squawk's ban-drop-default guards against clients that insert without the column; both
    # writers name it (credentials.vault), and old code never runs on this schema head.
    op.execute(
        "-- squawk-ignore ban-drop-default\n"
        f"ALTER TABLE {TABLE} ALTER COLUMN lifecycle_status DROP DEFAULT")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_exchange_account_credentials_lifecycle_status")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_exchange_account_credentials_active_verified")
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT ck_exchange_account_credentials_active_user_id")
    op.execute(
        f"ALTER TABLE {TABLE} DROP CONSTRAINT "
        "ck_exchange_account_credentials_bitfinex_user_id_positive")
    # squawk's NOT VALID, NOT NULL and unique-constraint rules guard against a long scan or
    # index build under the lock; the table holds a row per key and nothing reads it while
    # migrations run (see the docstring).
    op.execute(
        "-- squawk-ignore constraint-missing-not-valid\n"
        f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_exchange_account_credentials_lifecycle_status "
        "CHECK (lifecycle_status IN ('active', 'retired'))")
    op.execute(
        "-- squawk-ignore constraint-missing-not-valid\n"
        f"ALTER TABLE {TABLE} ADD CONSTRAINT "
        "ck_exchange_account_credentials_bitfinex_user_id_positive CHECK (bitfinex_user_id > 0)")
    op.execute(
        "-- squawk-ignore adding-not-nullable-field\n"
        f"ALTER TABLE {TABLE} ALTER COLUMN verified_at SET NOT NULL")
    op.execute(
        "-- squawk-ignore adding-not-nullable-field\n"
        f"ALTER TABLE {TABLE} ALTER COLUMN bitfinex_user_id SET NOT NULL")
    op.execute(
        "-- squawk-ignore constraint-missing-not-valid, disallowed-unique-constraint\n"
        f"ALTER TABLE {TABLE} ADD CONSTRAINT uq_exchange_account_credentials_venue_api_key "
        "UNIQUE (venue, api_key)")
    # squawk's ban-drop-column guards against clients still reading the column; none does
    # (this release removes the last reader, and old code never runs on this schema head).
    op.execute(
        "-- squawk-ignore ban-drop-column\n"
        f"ALTER TABLE {TABLE} DROP COLUMN last_verify_error")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
