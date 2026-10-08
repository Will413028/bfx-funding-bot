"""Refuse a REPEATABLE READ writer of trading_state; one scope index instead of two.

``guard_trading_state_transition`` takes the scope's advisory lock and then reads the latest
row to judge the transition. Under REPEATABLE READ the snapshot is fixed at the
transaction's first statement, before the lock is granted, so a writer that waited for
another sees the row before it and judges against a stale state (two concurrent automatic
resumes would both pass the window count). READ COMMITTED takes a new snapshot per
statement and SERIALIZABLE aborts one of the two, so the guard now refuses only REPEATABLE
READ, first, with ``BX004``. The bot writes on READ COMMITTED; its REPEATABLE READ sessions
are READ ONLY and never insert.

``ix_trading_state_scope_id`` (account, environment, id) and ``uq_trading_state_scope``
(id, account, environment) index the same columns; the unique one exists only for the
cancel-all audit's composite foreign key. The scope index becomes unique, the constraint
goes, and the foreign key is rebuilt on it (a referenced key matches a unique index of the
same columns in any order).

Preconditions: the guard is still 8e4b2f6a1c37's (no isolation check yet), both keys and
the foreign key exist as 5e820d6dc7da left them. Postconditions: one unique valid index on
the three columns, the foreign key validated on it, and the guard refusing REPEATABLE READ.

Compatibility: the previous web API image names neither index and never writes
``trading_state``. Forward-only: the downgrade raises
(docs/adr/2026-10-08-forward-only-migrations.md).

Revision ID: 2e835b6f4c12
Revises: 41cec7caf291
"""

from sqlalchemy import text

from alembic import op

revision = "2e835b6f4c12"
down_revision = "41cec7caf291"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

SCOPE_INDEX = "ix_trading_state_scope_id"
SCOPE_KEY = "uq_trading_state_scope"
AUDIT_FK = "fk_funding_cancel_all_audit_trading_state"
ISOLATION_CHECK = "current_setting('transaction_isolation') = 'repeatable read'"

# 8e4b2f6a1c37's guard with the isolation check first.
_TRANSITION_GUARD = f"""CREATE OR REPLACE FUNCTION public.guard_trading_state_transition()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE prev public.trading_state%ROWTYPE;
            db_now bigint := (extract(epoch FROM clock_timestamp()) * 1000)::bigint;
    BEGIN
      -- The latest row is read after the lock below; a REPEATABLE READ snapshot predates
      -- the lock and would judge the transition against a stale state.
      IF {ISOLATION_CHECK} THEN
        RAISE EXCEPTION 'trading_state is written under READ COMMITTED or SERIALIZABLE'
          USING ERRCODE = 'BX004';
      END IF;
      PERFORM pg_advisory_xact_lock(hashtextextended(
        'bfx-trading-state:' || NEW.exchange_account_id::text || ':' || NEW.deployment_environment, 0));
      -- Assigned under the scope lock: a row that waited here must not keep an
      -- id drawn before the row it waited for, or "highest id" would not be
      -- the latest decision.
      NEW.id := nextval('public.trading_state_id_seq');
      SELECT * INTO prev FROM public.trading_state
        WHERE exchange_account_id = NEW.exchange_account_id
          AND deployment_environment = NEW.deployment_environment
        ORDER BY id DESC LIMIT 1;
      IF NEW.cause = 'auto' THEN
        NEW.created_at_ms := db_now;
      END IF;
      IF NEW.state = 'ACTIVE' AND NEW.cause = 'auto' THEN
        IF NOT FOUND OR prev.state <> 'HALTED' OR prev.cause <> 'auto' THEN
          RAISE EXCEPTION 'illegal trading state transition % -> ACTIVE by auto',
            coalesce(prev.state || '/' || prev.cause, 'none') USING ERRCODE = 'BX001';
        END IF;
        IF db_now - prev.created_at_ms < 900000 THEN
          RAISE EXCEPTION 'auto resume before the minimum halt duration' USING ERRCODE = 'BX002';
        END IF;
        IF (SELECT count(*) FROM public.trading_state
              WHERE exchange_account_id = NEW.exchange_account_id
                AND deployment_environment = NEW.deployment_environment
                AND state = 'ACTIVE' AND cause = 'auto'
                AND created_at_ms > db_now - 86400000
            ) >= 2 THEN
          RAISE EXCEPTION 'auto resume limit reached for the window' USING ERRCODE = 'BX003';
        END IF;
      ELSIF FOUND AND prev.state <> 'ACTIVE' AND NEW.state = 'ACTIVE' AND NEW.cause <> 'operator' THEN
        RAISE EXCEPTION 'illegal trading state transition % -> ACTIVE by %', prev.state, NEW.cause;
      END IF;
      RETURN NEW;
    END $$"""

_GUARD_SOURCE = ("SELECT prosrc FROM pg_proc "
                 "WHERE oid = 'public.guard_trading_state_transition()'::regprocedure")
# Every index of trading_state over exactly the scope columns, with its uniqueness and validity.
_SCOPE_INDEXES = """
    SELECT i.indexrelid::regclass::text, i.indisunique, i.indisvalid
    FROM pg_index i
    WHERE i.indrelid = 'public.trading_state'::regclass AND i.indpred IS NULL
      AND i.indexprs IS NULL AND i.indnkeyatts = 3
      AND (SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_attribute a
           WHERE a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey))
          = ARRAY['deployment_environment', 'exchange_account_id', 'id']
    ORDER BY 1"""
# The audit foreign key: its validation and the index it is enforced through.
_AUDIT_FK = """
    SELECT c.convalidated, c.conindid::regclass::text FROM pg_constraint c
    WHERE c.conrelid = 'public.funding_cancel_all_audit'::regclass AND c.conname = :name
      AND c.contype = 'f' AND c.confrelid = 'public.trading_state'::regclass"""


def _scalar(sql: str, **params: object) -> object:
    return op.get_bind().execute(text(sql), params).scalar()


def upgrade() -> None:
    conn = op.get_bind()
    source = _scalar(_GUARD_SOURCE)
    if source is None or "transaction_isolation" in str(source) or "BX003" not in str(source):
        raise RuntimeError("guard_trading_state_transition is not 8e4b2f6a1c37's; refuse to "
                           "replace it")
    before = conn.execute(text(_SCOPE_INDEXES)).all()
    if [tuple(row) for row in before] != [(SCOPE_INDEX, False, True), (SCOPE_KEY, True, True)]:
        raise RuntimeError(f"trading_state scope indexes are not as 5e820d6dc7da left them: {before}")
    if conn.execute(text(_AUDIT_FK), {"name": AUDIT_FK}).first() != (True, SCOPE_KEY):
        raise RuntimeError(f"{AUDIT_FK} is not enforced through {SCOPE_KEY}")

    op.execute(_TRANSITION_GUARD)
    # The new key exists before the old one goes, so the foreign key is never without one.
    op.execute(f"CREATE UNIQUE INDEX {SCOPE_INDEX}_unique ON public.trading_state "
               "(exchange_account_id, deployment_environment, id)")
    op.execute(f"ALTER TABLE public.funding_cancel_all_audit DROP CONSTRAINT {AUDIT_FK}")
    op.execute(f"ALTER TABLE public.trading_state DROP CONSTRAINT {SCOPE_KEY}")
    op.execute(f"DROP INDEX public.{SCOPE_INDEX}")
    op.execute(f"ALTER INDEX public.{SCOPE_INDEX}_unique RENAME TO {SCOPE_INDEX}")
    op.execute(
        f"ALTER TABLE public.funding_cancel_all_audit ADD CONSTRAINT {AUDIT_FK} "
        "FOREIGN KEY (trading_state_id, exchange_account_id, deployment_environment) "
        "REFERENCES public.trading_state (id, exchange_account_id, deployment_environment) "
        "ON DELETE RESTRICT"
    )

    after = conn.execute(text(_SCOPE_INDEXES)).all()
    if [tuple(row) for row in after] != [(SCOPE_INDEX, True, True)]:
        raise RuntimeError(f"trading_state scope indexes after the upgrade: {after}")
    if conn.execute(text(_AUDIT_FK), {"name": AUDIT_FK}).first() != (True, SCOPE_INDEX):
        raise RuntimeError(f"{AUDIT_FK} is not validated through {SCOPE_INDEX}")
    if ISOLATION_CHECK not in str(_scalar(_GUARD_SOURCE)):
        raise RuntimeError("guard_trading_state_transition does not refuse REPEATABLE READ")


def downgrade() -> None:
    raise RuntimeError(
        "2e835b6f4c12 is forward-only (docs/adr/2026-10-08-forward-only-migrations.md): "
        "restore the pre-migration backup to roll back"
    )
