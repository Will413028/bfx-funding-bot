"""Drop ``api_keys``, the SP2 per-user Bitfinex key table nothing reads any more.

Exchange credentials live in ``exchange_account_credentials`` (account UUID as AAD); the
webapi lost key writes in 9d and the last code that read ``api_keys`` went in R7a. Production
holds no row (counted on 2026-10-25), so the drop loses nothing. Its grants and the two
foreign keys it holds (to ``user_profiles`` and ``exchange_accounts``) go with it; no foreign
key points into it.

Refusals, each leaving the table and the revision as they were:

* a row in ``api_keys``: a database that still holds a key is not this one;
* a function body that reads it: ``DROP TABLE`` does not see those, and the function would
  fail on its next call. A view or another object that depends on the table refuses on its
  own: the drop is RESTRICT, not CASCADE.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: f486e11435c9
Revises: 638455da00bb
"""

from sqlalchemy import text

from alembic import op

revision = "f486e11435c9"
down_revision = "638455da00bb"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_FUNCTIONS_READING_API_KEYS = r"""
SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND p.prosrc ~* '(\m|")api_keys(\M|")'
"""


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("LOCK TABLE public.api_keys IN ACCESS EXCLUSIVE MODE")
    rows = int(bind.execute(text("SELECT count(*) FROM public.api_keys")).scalar() or 0)
    if rows:
        raise RuntimeError(f"refuse: api_keys holds {rows} rows")
    functions = int(bind.execute(text(_FUNCTIONS_READING_API_KEYS)).scalar() or 0)
    if functions:
        raise RuntimeError(f"refuse: {functions} function bodies read api_keys")
    # squawk's ban-drop-table guards against clients still using the table; none does
    # (R7a removed the last reader) and the table is empty, checked above under the lock.
    op.execute("-- squawk-ignore ban-drop-table\nDROP TABLE public.api_keys")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
