"""A JSON column holds a document or SQL NULL, never the JSON literal ``null``.

The models' JSON type stored Python ``None`` as ``'null'::jsonb`` (SQLAlchemy's default without
``none_as_null``), which ``IS NULL`` does not see: a live attempt's ``seed_provenance`` read as
seeded, ``ck_submission_attempt_policy_or_seed`` accepted it as provenance, and every
``block``/``scope_block``/``flags`` stored the literal. Since this release every JSON column
is ``core.db.JSON_DOCUMENT``, which writes SQL NULL and declares the CHECK below. This revision:

* turns ``fill_rate_model_artifacts.metadata_json`` from ``json`` into ``jsonb`` (the one
  column of the other type; nothing relies on ``json``'s verbatim text);
* rewrites every stored JSON ``null`` of the columns below to SQL NULL. The ledger tables are
  append-only by trigger and the mirrors guarded, so a table with rows to rewrite has its user
  triggers disabled for its one UPDATE (the bot is stopped while a deploy migrates, and the
  value reads back as ``None`` either way). A NOT NULL column cannot hold the SQL NULL, so a
  literal there refuses the upgrade instead (production has none);
* adds ``ck_<table>_<column>_json`` (``jsonb_typeof(column) <> 'null'``) to each, validated;
* makes ``ck_submission_attempt_policy_or_seed`` exactly one of the two: a live attempt names
  its policy revision, a seeded one its legacy provenance.

The rewrite changes stored bytes of append-only rows. ``ledger_digest`` compares a restored
copy with production row by row, so a restore drill that stops before this migration (a named
``backup_label``/``target_time``) no longer matches production; drills that replay the
archive past it, and every backup taken after it, do (docs/runbooks/offsite-dr.md).

Downgrade drops the new CHECKs, restores the old seed CHECK and the ``json`` type; the
rewritten rows stay SQL NULL (they read back the same).

Revision ID: a6c7e8f9b0d1
Revises: f5a6b7c8d9e0
"""

from alembic import op

revision = "a6c7e8f9b0d1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None
# Rewrites values the projector and archive never read; no cursor or archive change.
ledger_contract = "preserved"

# Every written JSON column of ``public``, with its nullability (pinned to the models by
# tests/core/test_json_columns.py).
COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("accepted_capital_basis", "scope_block", True),
    ("accepted_capital_basis_symbol", "block", True),
    ("account_config_drafts", "config", False),
    ("capital_authority_epoch", "evidence", True),
    ("capital_policy_revisions", "policy", False),
    ("capital_policy_revisions", "source", False),
    ("diagnostics", "payload", False),
    ("execution_decisions", "model_evidence", False),
    ("execution_decisions", "safety_result", False),
    ("execution_resolution_journal", "evidence", False),
    ("fill_rate_model_artifacts", "metadata_json", False),
    ("funding_book_snapshots", "payload", False),
    ("ledger_observation", "evidence", False),
    ("ledger_observation_credit", "flags", True),
    ("ledger_observation_credit", "raw", False),
    ("ledger_observation_credit_history", "flags", True),
    ("ledger_observation_credit_history", "raw", False),
    ("ledger_observation_offer", "flags", True),
    ("ledger_observation_offer", "raw", False),
    ("ledger_observation_offer_history", "flags", True),
    ("ledger_observation_offer_history", "raw", False),
    ("quarantine_opening", "evidence", False),
    ("sim_venue_event", "payload", False),
    ("submission_attempt_journal", "authorization_evidence", False),
    ("submission_attempt_journal", "normalized_payload", False),
    ("submission_attempt_journal", "seed_provenance", True),
    ("transport_outcome_journal", "evidence", False),
    ("user_configs", "config", False),
    ("venue_credit_mirror", "flags", True),
    ("venue_offer_mirror", "flags", True),
)
JSON_TO_JSONB = ("fill_rate_model_artifacts", "metadata_json")
SEED_CHECK = "ck_submission_attempt_policy_or_seed"
SEED_RULE = "(policy_revision_id IS NULL) <> (seed_provenance IS NULL)"
_PREVIOUS_SEED_RULE = "policy_revision_id IS NOT NULL OR seed_provenance IS NOT NULL"


def check_name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_json"


def _rewrite(table: str, column: str) -> str:
    # DISABLE/ENABLE TRIGGER USER would re-enable a trigger someone disabled on purpose.
    return f"""
            IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.{table}'::regclass
                       AND NOT tgisinternal AND tgenabled = 'D') THEN
              RAISE EXCEPTION 'refuse to rewrite public.{table}: a trigger is disabled';
            END IF;
            ALTER TABLE public.{table} DISABLE TRIGGER USER;
            UPDATE public.{table} SET {column} = NULL WHERE jsonb_typeof({column}) = 'null';
            ALTER TABLE public.{table} ENABLE TRIGGER USER;"""


def upgrade() -> None:
    table, column = JSON_TO_JSONB
    op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} DROP DEFAULT")
    op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb")
    op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} SET DEFAULT '{{}}'::jsonb")
    for table, column, nullable in COLUMNS:
        refusal = f"""
            RAISE EXCEPTION 'refuse to rewrite public.{table}.{column}: NOT NULL, and % row(s) '
              'hold the JSON literal null',
              (SELECT count(*) FROM public.{table} WHERE jsonb_typeof({column}) = 'null');"""
        op.execute(f"""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM public.{table} WHERE jsonb_typeof({column}) = 'null') THEN
            {_rewrite(table, column) if nullable else refusal}
          END IF;
        END $$""")
        op.execute(
            f"ALTER TABLE public.{table} ADD CONSTRAINT {check_name(table, column)} "
            f"CHECK (jsonb_typeof({column}) <> 'null')"
        )
    op.execute(f"ALTER TABLE public.submission_attempt_journal DROP CONSTRAINT {SEED_CHECK}")
    op.execute(
        f"ALTER TABLE public.submission_attempt_journal ADD CONSTRAINT {SEED_CHECK} "
        f"CHECK ({SEED_RULE})"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE public.submission_attempt_journal DROP CONSTRAINT {SEED_CHECK}")
    op.execute(
        f"ALTER TABLE public.submission_attempt_journal ADD CONSTRAINT {SEED_CHECK} "
        f"CHECK ({_PREVIOUS_SEED_RULE})"
    )
    for table, column, _ in COLUMNS:
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT {check_name(table, column)}")
    table, column = JSON_TO_JSONB
    op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} DROP DEFAULT")
    op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} TYPE json USING {column}::json")
    op.execute(f"ALTER TABLE public.{table} ALTER COLUMN {column} SET DEFAULT '{{}}'::json")
