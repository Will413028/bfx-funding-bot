"""Drop ``accepted_capital_basis_symbol.unattributed_credits`` and
``accepted_capital_basis_credit.attribution_basis``.

A basis no longer attributes a lent credit to a cell
(docs/adr/2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md), so nothing reads or
writes either column (docs/plans/2026-11-14-cell-limit-resting-offers.md, step 3, D1). The rows
stay: the basis tables are insert-once and their immutability triggers fire on UPDATE, DELETE
and TRUNCATE, not on DDL. What is lost is the attribution each earlier basis recorded, and with
it the inputs of those bases' ``digest`` (nothing recomputes one).

In order, all in the revision's transaction:

1. Lock both tables, count their rows and record which columns of each the web API reads.
2. Drop the two CHECKs that name a dropped column: ``ck_accepted_basis_symbol_amount`` (it also
   bounds ``available``, ``offered``, ``credits`` and ``foreign_offers``) and
   ``ck_accepted_basis_credit_attribution``.
3. ``DROP COLUMN`` on both tables. The web API's column grant on ``unattributed_credits`` goes
   with the column.
4. Rebuild ``ck_accepted_basis_symbol_amount`` under its name without the dropped column.
5. Assert the end state: neither column exists, both row counts are unchanged, the amount CHECK
   is back and the attribution CHECK is gone, no function body outside the archive schemas
   names a dropped column (a PL/pgSQL body is not a dependency PostgreSQL tracks, so the drop would not refuse it), and
   the web API reads the columns it read in step 1 less the dropped ones.

squawk: ``DROP COLUMN`` and ``ADD CONSTRAINT ... CHECK`` without ``NOT VALID`` are marked
``squawk-ignore``. Those rules guard against breaking a client that still reads the column and
against blocking writers on a live table; bfx-deploy stops the webapi and every writer while
migrations run, every build refuses a schema head other than its own, and ``NOT VALID`` plus
``VALIDATE`` in one transaction would not shorten the lock (precedent: 545b2877e9bd).

The downgrade raises: the dropped values are gone
(docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: dc9d9290c2c0
Revises: 545b2877e9bd
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "dc9d9290c2c0"
down_revision = "545b2877e9bd"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

SYMBOL = "accepted_capital_basis_symbol"
CREDIT = "accepted_capital_basis_credit"
# (table, column, the CHECK that names it)
DROPPED: tuple[tuple[str, str, str], ...] = (
    (SYMBOL, "unattributed_credits", "ck_accepted_basis_symbol_amount"),
    (CREDIT, "attribution_basis", "ck_accepted_basis_credit_attribution"),
)
AMOUNT_CHECK = "ck_accepted_basis_symbol_amount"
AMOUNT_CHECK_SQL = "available >= 0 AND offered >= 0 AND credits >= 0 AND foreign_offers >= 0"
WEBAPI = "bfx_webapi"
ARCHIVE_SCHEMAS = ("legacy_archive", "release_archive")


def _row_counts() -> dict[str, int]:
    bind = op.get_bind()
    return {table: int(bind.execute(text(f"SELECT count(*) FROM public.{table}")).scalar() or 0)
            for table in (SYMBOL, CREDIT)}


def _webapi_reads() -> dict[str, set[str]]:
    """The columns of each table the web API can SELECT; empty where the role does not exist."""
    bind = op.get_bind()
    if not bind.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": WEBAPI}).scalar():
        return {SYMBOL: set(), CREDIT: set()}
    return {table: set(bind.execute(text(
        "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(:table) AND attnum > 0 "
        "AND NOT attisdropped AND has_column_privilege(:r, attrelid, attnum, 'SELECT')"
    ), {"table": f"public.{table}", "r": WEBAPI}).scalars()) for table in (SYMBOL, CREDIT)}


def _checks() -> set[str]:
    return set(op.get_bind().execute(text(
        "SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname = ANY(:names) "
        "AND conrelid = ANY(ARRAY[to_regclass(:symbol), to_regclass(:credit)]::oid[])"
    ), {"names": [check for *_, check in DROPPED],
        "symbol": f"public.{SYMBOL}", "credit": f"public.{CREDIT}"}).scalars())


def _assert_end_state(rows: dict[str, int], reads: dict[str, set[str]]) -> None:
    bind = op.get_bind()
    left = [f"{table}.{column}" for table, column, _ in DROPPED if bind.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = :table AND column_name = :column"
    ), {"table": table, "column": column}).scalar()]
    if left:
        raise RuntimeError(f"refuse: column still present: {', '.join(left)}")
    if _row_counts() != rows:
        raise RuntimeError(f"refuse: row counts changed from {rows} to {_row_counts()}")
    if _checks() != {AMOUNT_CHECK}:
        raise RuntimeError(f"refuse: CHECKs on the basis tables are {sorted(_checks())}, "
                           f"expected only {AMOUNT_CHECK}")
    readers = list(bind.execute(text(
        "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE p.prosrc ~* :columns AND n.nspname <> ALL (:archives) "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
    ), {"columns": '(\\m|")(' + "|".join(column for _, column, _ in DROPPED) + ')(\\M|")',
        "archives": list(ARCHIVE_SCHEMAS)}).scalars())
    if readers:
        raise RuntimeError(f"refuse: functions still read a dropped column: {', '.join(readers)}")
    expected = {table: reads[table] - {column for t, column, _ in DROPPED if t == table}
                for table in reads}
    if _webapi_reads() != expected:
        raise RuntimeError(f"refuse: {WEBAPI} reads {_webapi_reads()}, expected {expected}")


def upgrade() -> None:
    # The counts are compared at the end: taken under the lock, no writer can move them.
    op.execute(f"LOCK TABLE public.{SYMBOL}, public.{CREDIT} IN ACCESS EXCLUSIVE MODE")
    rows, reads = _row_counts(), _webapi_reads()
    for table, _, check in DROPPED:
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {check}")
    for table, column, _ in DROPPED:
        op.execute("-- squawk-ignore ban-drop-column\n"
                   f"ALTER TABLE public.{table} DROP COLUMN {column}")
    op.execute("-- squawk-ignore constraint-missing-not-valid\n"
               f"ALTER TABLE public.{SYMBOL} ADD CONSTRAINT {AMOUNT_CHECK} "
               f"CHECK ({AMOUNT_CHECK_SQL})")
    _assert_end_state(rows, reads)


def downgrade() -> None:
    raise RuntimeError(
        "dc9d9290c2c0 is irreversible: the dropped attribution values are gone "
        "(docs/adr/2026-10-08-forward-only-migrations.md)")
