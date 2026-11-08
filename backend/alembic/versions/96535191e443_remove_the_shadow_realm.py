"""Remove the ``shadow`` realm: a database is stamped ``prod`` or ``ci``.

After the simulation soak was retired (ADR 2026-11-08 retire the simulation soak) nothing
writes realm ``shadow``; the simulated venue runs only in ``ci`` (ADR 2026-11-08 remove the
shadow realm). Two CHECKs narrow:

* ``database_realm.ck_database_realm_realm``: ``realm IN ('prod', 'ci')``;
* ``sim_venue_event.ck_sim_venue_event_realm``: ``deployment_environment = 'ci'``.

Refusals, each leaving the schema and the revision as they were:

* a database stamped ``shadow``;
* a ``shadow`` row in any table of the current schema with a ``deployment_environment``
  column (read from ``information_schema``, not a copied list).

The new CHECKs are added NOT VALID and validated in the same revision; both tables are tiny
and bfx-deploy stops the webapi and the bot while migrations run.

The downgrade restores the wider CHECKs; widening loses nothing, so it is written
(docs/adr/2026-10-08-forward-only-migrations.md D1 allows a lossless reverse).

Revision ID: 96535191e443
Revises: a9d2c4e6f813
"""
from __future__ import annotations

from sqlalchemy import text

from alembic import op

revision = "96535191e443"
down_revision = "a9d2c4e6f813"
branch_labels = None
depends_on = None
# No projection table, cursor or event_log content changes (core/schema_head.py).
ledger_contract = "preserved"

_REALM_TABLES = """
SELECT table_name FROM information_schema.columns
WHERE table_schema = current_schema() AND column_name = 'deployment_environment'
  AND table_name IN (SELECT table_name FROM information_schema.tables
                     WHERE table_schema = current_schema() AND table_type = 'BASE TABLE')
ORDER BY table_name
"""


def _refuse_shadow() -> None:
    bind = op.get_bind()
    stamped = int(bind.execute(text(
        "SELECT count(*) FROM database_realm WHERE realm = 'shadow'")).scalar() or 0)
    if stamped:
        raise RuntimeError("refuse: this database is stamped realm 'shadow', which no longer exists")
    holding = [
        table for table in bind.execute(text(_REALM_TABLES)).scalars()
        if bind.execute(text(
            f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE deployment_environment = \'shadow\')'
        )).scalar()
    ]
    if holding:
        raise RuntimeError(f"refuse: rows of realm 'shadow' in {', '.join(holding)}")


def _replace(table: str, name: str, check: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check}) NOT VALID")
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    _refuse_shadow()
    _replace("database_realm", "ck_database_realm_realm", "realm IN ('prod', 'ci')")
    _replace("sim_venue_event", "ck_sim_venue_event_realm", "deployment_environment = 'ci'")


def downgrade() -> None:
    _replace("database_realm", "ck_database_realm_realm", "realm IN ('prod', 'shadow', 'ci')")
    _replace("sim_venue_event", "ck_sim_venue_event_realm",
             "deployment_environment IN ('shadow', 'ci')")
