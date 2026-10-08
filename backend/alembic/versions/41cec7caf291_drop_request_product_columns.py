"""Drop the operator request tables' product columns (R2').

ADR 2026-10-08 operator-requests-keep-state-effects-carry-request-id (D8, D9, D10). Since
5e820d6dc7da an effect names its request (``operator_request_id`` on the trading state and the
policy revision); the request's own ``trading_state_id`` and ``policy_revision_id`` are closed
there (unmapped, no runtime UPDATE grant, no CHECK names them) and dropped here, with their
foreign keys. The outcome CHECKs stay: they no longer reference the columns.

Precondition (D7'): nothing the dropped columns say is lost. Every applied request whose
column names a product either is the product's cause, and the product names it back, or only
restated a decision someone else wrote (trading: the row is not the operator row this request
wrote by the back-fill key of 5e820d6dc7da, or an earlier request wrote it; capital: the
revision's ``source.request_id`` names another request, an ``unchanged`` request). Requests
applied since 5e820d6dc7da left both columns NULL. Any violation refuses the revision with its
count; nothing has changed by then.

Compatibility: the previous web API image (5e820d6dc7da's) maps neither column. Irreversible:
the downgrade raises (docs/adr/2026-10-08-forward-only-migrations.md); a restore of the
pre-migration backup is the only way back.

Revision ID: 41cec7caf291
Revises: 5e820d6dc7da
"""

from sqlalchemy import text

from alembic import op

revision = "41cec7caf291"
down_revision = "5e820d6dc7da"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

# request table -> its product column.
DROPPED_COLUMNS = {
    "trading_control_requests": "trading_state_id",
    "capital_policy_requests": "policy_revision_id",
}
OUTCOME_CHECKS = ("ck_trading_control_requests_outcome", "ck_capital_policy_requests_outcome")

# The operator row an applied trading request wrote (5e820d6dc7da's back-fill key); of several
# requests naming the row, the earliest processed wrote it.
_TRADING_WRITER = """
    SELECT DISTINCT ON (s.id) s.id, r.request_id
    FROM public.trading_control_requests r JOIN public.trading_state s
      ON s.id = r.trading_state_id AND s.cause = 'operator' AND s.actor = r.requested_by
     AND s.reason = (CASE r.action WHEN 'kill' THEN 'kill: ' ELSE 'resumed: ' END) || r.reason
    WHERE r.state = 'applied'
    ORDER BY s.id, r.processed_at_ms, r.request_id"""

PRECONDITIONS: tuple[tuple[str, str], ...] = (
    ("trading request that wrote its trading state, which does not name it",
     f"SELECT count(*) FROM ({_TRADING_WRITER}) AS w JOIN public.trading_state s ON s.id = w.id "
     "WHERE s.operator_request_id IS DISTINCT FROM w.request_id"),
    ("capital request that wrote its revision, which does not name it",
     "SELECT count(*) FROM public.capital_policy_requests r JOIN public.capital_policy_revisions v "
     "ON v.id = r.policy_revision_id "
     "WHERE v.source ->> 'request_id' = r.request_id::text "
     "AND v.operator_request_id IS DISTINCT FROM r.request_id"),
)


def _violations(checks: tuple[tuple[str, str], ...]) -> list[str]:
    conn = op.get_bind()
    found = [(what, conn.execute(text(sql)).scalar_one()) for what, sql in checks]
    return [f"{what}: {count}" for what, count in found if count]


def upgrade() -> None:
    violations = _violations(PRECONDITIONS)
    if violations:
        raise RuntimeError("refuse to drop the request product columns; rows whose link would be "
                           "lost: " + "; ".join(violations))
    for table, column in DROPPED_COLUMNS.items():
        op.execute(f"ALTER TABLE public.{table} DROP COLUMN {column}")
    # The table, not just the name: release_archive keeps an older table's CHECK of one name.
    kept = op.get_bind().execute(text(
        "SELECT count(*) FROM pg_constraint WHERE (conrelid::regclass::text, conname) IN "
        "(('trading_control_requests', 'ck_trading_control_requests_outcome'), "
        "('capital_policy_requests', 'ck_capital_policy_requests_outcome'))")).scalar_one()
    if kept != len(OUTCOME_CHECKS):
        raise RuntimeError(f"an outcome CHECK went with the dropped columns: {kept} of "
                           f"{len(OUTCOME_CHECKS)} left")


def downgrade() -> None:
    raise RuntimeError(
        "41cec7caf291 is forward-only (docs/adr/2026-10-08-forward-only-migrations.md): "
        "restore the pre-migration backup to roll back"
    )
