"""Remove the ``shadow`` realm: a database is stamped ``prod`` or ``ci``.

After the simulation soak was retired (ADR 2026-11-08 retire the simulation soak) nothing
writes realm ``shadow``; the simulated venue runs only in ``ci`` (ADR 2026-11-08 remove the
shadow realm). Two CHECKs narrow:

* ``database_realm.ck_database_realm_realm``: ``realm IN ('prod', 'ci')``;
* ``sim_venue_event.ck_sim_venue_event_realm``: ``deployment_environment = 'ci'``.

Refusals, each leaving the schema and the revision as they were:

* a database stamped ``shadow``;
* a ``shadow`` row in any base table, in any schema but ``pg_catalog`` and
  ``information_schema``, with a ``deployment_environment`` column (``public`` and also
  ``projection_audit``, ``legacy_archive``, ``release_archive``). The tables are read from
  ``pg_catalog`` (not a copied list) and probed in one statement, a ``UNION ALL`` of one
  ``EXISTS`` per table, so the answer comes from a single snapshot.

The new CHECKs are added NOT VALID and validated in the same revision because CI's squawk
lint (rule ``constraint-missing-not-valid``) rejects a plain ``ADD CONSTRAINT ... CHECK``;
the split buys nothing here, both tables are tiny and bfx-deploy stops the webapi and the bot
while migrations run.

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
SELECT n.nspname, c.relname FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE a.attname = 'deployment_environment' AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY 1, 2
"""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _refuse_shadow() -> None:
    bind = op.get_bind()
    stamped = int(bind.execute(text(
        "SELECT count(*) FROM database_realm WHERE realm = 'shadow'")).scalar() or 0)
    if stamped:
        raise RuntimeError("refuse: this database is stamped realm 'shadow', which no longer exists")
    tables = [(str(schema), str(table)) for schema, table in bind.execute(text(_REALM_TABLES))]
    if not tables:
        return
    probe = " UNION ALL ".join(
        f"SELECT '{schema}.{table}' AS t WHERE EXISTS (SELECT 1 FROM {_quote(schema)}.{_quote(table)} "
        "WHERE deployment_environment = 'shadow')"
        for schema, table in tables
    )
    holding = list(bind.execute(text(probe)).scalars())
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
