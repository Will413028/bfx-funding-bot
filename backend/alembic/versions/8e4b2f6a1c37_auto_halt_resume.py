"""auto halt resume

Revises 7d2a9c4e6b13. ADR 2026-09-26 auto-halt-resumes-when-condition-clears:
an automatic protection may end its own ``HALTED/auto``. The transition guard
keeps "only an operator ends a halt" for every other halt and admits
``HALTED/auto -> ACTIVE/auto`` only when

- the halt being ended is at least 15 minutes old, and
- fewer than two ``ACTIVE/auto`` rows fall in the rolling 24 hours,

both measured on the database clock. Every ``cause = 'auto'`` row is stamped
with that clock too (``created_at_ms`` is overwritten under the scope lock, as
``id`` is), so the bot -- the only writer of automatic rows -- cannot shorten a
halt or hide a resume from the window by the time it supplies. Operator rows keep
their writer's time. Rejections carry their own SQLSTATE (``BX001`` illegal
automatic resume, ``BX002`` too soon, ``BX003`` limit reached). Whether the
condition behind the halt has cleared is the bot's judgement (three clean accepted snapshots); the database bounds how soon and how
often. The constants are mirrored in ``safety/trading_state.py`` and kept in
step by ``test_trading_state_rule_parity``.

No grant changes: the bot already inserts ``trading_state`` rows.
"""
from alembic import op

revision = "8e4b2f6a1c37"
down_revision = "7d2a9c4e6b13"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_GUARD_HEAD = """CREATE OR REPLACE FUNCTION public.guard_trading_state_transition()
    RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE prev public.trading_state%ROWTYPE;
    BEGIN
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
"""

_TRANSITION_GUARD = _GUARD_HEAD.replace(
    "    DECLARE prev public.trading_state%ROWTYPE;",
    "    DECLARE prev public.trading_state%ROWTYPE;\n"
    "            db_now bigint := (extract(epoch FROM clock_timestamp()) * 1000)::bigint;",
) + """      IF NEW.cause = 'auto' THEN
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

# 5b1e7c9d2a40's guard, restored on downgrade.
_OLD_TRANSITION_GUARD = _GUARD_HEAD + """      IF FOUND AND prev.state <> 'ACTIVE' AND NEW.state = 'ACTIVE' AND NEW.cause <> 'operator' THEN
        RAISE EXCEPTION 'illegal trading state transition % -> ACTIVE by %', prev.state, NEW.cause;
      END IF;
      RETURN NEW;
    END $$"""


def upgrade() -> None:
    op.execute(_TRANSITION_GUARD)


def downgrade() -> None:
    op.execute(_OLD_TRANSITION_GUARD)
