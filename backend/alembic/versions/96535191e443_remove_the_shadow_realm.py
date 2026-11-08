"""Remove the ``shadow`` realm: a database is stamped ``prod`` or ``ci``.

After the simulation soak was retired (ADR 2026-11-08 retire the simulation soak) nothing
writes realm ``shadow``; the simulated venue runs only in ``ci`` (ADR 2026-11-08 remove the
shadow realm). Two CHECKs narrow:

* ``database_realm.ck_database_realm_realm``: ``realm IN ('prod', 'ci')``;
* ``sim_venue_event.ck_sim_venue_event_realm``: ``deployment_environment = 'ci'``.

Refusals, each leaving the schema and the revision as they were:

* a database stamped ``shadow``;
* a ``shadow`` row in any realm-guarded table: one carrying the ``guard_database_realm``
  trigger (a7c3e9f1b2d4), in any schema. Unguarded tables (``release_archive``'s frozen copies)
  are not realm data and do not block. The tables are read from ``pg_catalog`` (not a copied
  list), the probe text is built in SQL with ``format()`` quoting, and it runs as one statement,
  a ``UNION ALL`` of one ``EXISTS`` per table, so the answer comes from a single snapshot.

The new CHECKs are added with a plain ``ADD CONSTRAINT`` under a ``squawk-ignore`` for
``constraint-missing-not-valid`` (precedent: c3e7a91d5b20). That rule guards against a long
validating scan under the lock; NOT VALID plus VALIDATE in one transaction would not avoid it
either (squawk flags that too), both tables are tiny, and bfx-deploy stops the webapi and the
bot while migrations run, so nothing waits on the lock.

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

# The realm-guarded tables are the ones carrying the realm trigger (a7c3e9f1b2d4,
# ``guard_database_realm``), in any schema. Tables without it (release_archive's frozen copies,
# unguarded scratch tables) are not live realm data and are not scanned; a partition is covered
# by its parent. The probe text is built here from the catalog, names quoted by format().
_PROBE = """
SELECT string_agg(
  format('SELECT %L AS t WHERE EXISTS (SELECT 1 FROM %I.%I WHERE deployment_environment = %L)',
         format('%I.%I', n.nspname, c.relname), n.nspname, c.relname, 'shadow'),
  ' UNION ALL ' ORDER BY n.nspname, c.relname)
FROM pg_catalog.pg_trigger t
JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE t.tgfoid = 'public.guard_database_realm'::regproc AND NOT t.tgisinternal
  AND c.relkind IN ('r', 'p') AND NOT c.relispartition
"""


def _refuse_shadow() -> None:
    bind = op.get_bind()
    stamped = int(bind.execute(text(
        "SELECT count(*) FROM database_realm WHERE realm = 'shadow'")).scalar() or 0)
    if stamped:
        raise RuntimeError("refuse: this database is stamped realm 'shadow', which no longer exists")
    probe = bind.execute(text(_PROBE)).scalar()
    if not probe:
        return
    holding = list(bind.exec_driver_sql(str(probe)).scalars())
    if holding:
        raise RuntimeError(f"refuse: rows of realm 'shadow' in {', '.join(holding)}")


def _replace(table: str, name: str, check: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute("-- squawk-ignore constraint-missing-not-valid\n"
               f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check})")


def upgrade() -> None:
    _refuse_shadow()
    _replace("database_realm", "ck_database_realm_realm", "realm IN ('prod', 'ci')")
    _replace("sim_venue_event", "ck_sim_venue_event_realm", "deployment_environment = 'ci'")


def downgrade() -> None:
    _replace("database_realm", "ck_database_realm_realm", "realm IN ('prod', 'shadow', 'ci')")
    _replace("sim_venue_event", "ck_sim_venue_event_realm",
             "deployment_environment IN ('shadow', 'ci')")
