"""Per-symbol conservation verdict on the accepted capital basis.

Acceptance compares each symbol of a new basis with the same symbol of the
scope's previous accepted basis (lent change against the venue offers' drop,
less what foreign offers executed) and stores the verdict with the basis, so
nothing is carried in memory between observations. The verdict is three columns
of ``accepted_capital_basis_symbol`` rather than a sibling table: it is one fact
per row of that table, written in the same INSERT, and the table's immutability
triggers (``immutable_ledger_write`` / ``immutable_ledger_truncate``) already
cover every column.

Grants: ``bfx_bot`` holds table-level SELECT and INSERT on the table, which
covers the new columns; ``bfx_webapi`` and the cutover reader hold column-level
allowlists that this revision deliberately leaves unchanged (no consumer reads
the verdict outside the bot yet).

Existing rows: a symbol row written before this revision was compared with nothing,
which is exactly what ``baseline`` says, so the upgrade backfills ``baseline`` / 0 / 0 through
a column default that it drops again (an UPDATE would trip the immutability trigger; the
default fills the rows as the column is added). The next basis compares with the old row's
amounts as usual. The downgrade drops the columns and so refuses a database holding any
verdict other than ``baseline``: that is stored evidence, not something to rebuild.

Revision ID: a3b4c5d6e7f8
Revises: e1f2a3b4c5d7
"""

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "a3b4c5d6e7f8"
down_revision = "e1f2a3b4c5d7"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_TABLE = "accepted_capital_basis_symbol"
_CHECK = "ck_accepted_basis_symbol_conservation"
# The model's expression (``modules/ledger/tables.py``).
_CONSERVATION = (
    "lent_unexplained >= 0 AND foreign_executed >= 0 AND CASE conservation "
    "WHEN 'baseline' THEN lent_unexplained = 0 AND foreign_executed = 0 "
    "WHEN 'conserved' THEN true "
    "WHEN 'foreign_lending' THEN lent_unexplained > 0 AND foreign_executed > 0 "
    "WHEN 'unexplained_lending' THEN lent_unexplained > 0 "
    "ELSE false END"
)


def _refuse_verdicts() -> None:
    if (
        op.get_bind()
        .execute(text(f"SELECT EXISTS (SELECT 1 FROM public.{_TABLE} WHERE conservation <> 'baseline')"))
        .scalar()
    ):
        raise RuntimeError(f"refuse downgrade of populated ledger: {_TABLE} holds conservation verdicts")


def upgrade() -> None:
    op.add_column(
        _TABLE, sa.Column("conservation", sa.Text(), nullable=False, server_default="baseline")
    )
    op.add_column(
        _TABLE, sa.Column("lent_unexplained", sa.Numeric(), nullable=False, server_default="0")
    )
    op.add_column(
        _TABLE, sa.Column("foreign_executed", sa.Numeric(), nullable=False, server_default="0")
    )
    for column in ("conservation", "lent_unexplained", "foreign_executed"):
        op.alter_column(_TABLE, column, server_default=None)
    op.create_check_constraint(_CHECK, _TABLE, _CONSERVATION)


def downgrade() -> None:
    _refuse_verdicts()
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, "foreign_executed")
    op.drop_column(_TABLE, "lent_unexplained")
    op.drop_column(_TABLE, "conservation")
