"""``bfx_research``: the weekly report's and the research jobs' role, an exact allowlist.

The weekly measurement chain (``deploy/vm/ops/docker-compose.weekly-report.yml``) and the
manual research one-shot jobs (``docs/runbooks/research-one-shot-jobs.md``) used the bot's
role, which may write the ledger and trading state. Their own role may write only the
research tables, with the privileges their statements need:

* ``funding_stats``, ``perp_funding_rates``, ``liquidations``: INSERT ... ON CONFLICT DO
  UPDATE (``upsert_funding_stats``, ``upsert_perp_funding``, ``upsert_liquidations``);
* ``attribution_weekly``: DELETE the realm's rows, then INSERT (``persist_rows``);
* ``fill_rate_stats``: DELETE one (source, symbol, period) model, then INSERT
  (``learn_book_and_store``); its rows reference ``fill_rate_model_artifacts``, so that table
  gets INSERT too (``ensure_fill_model_artifact``, insert-if-absent; never UPDATE or DELETE).

SELECT is table level on what the steps read: the tables above, and the inputs of the
attribution, realized interest and G3 reports (venue read models, the journals' cell
provenance, candles, the book snapshots). No credential, vault, request outbox,
``trading_state`` or ``deployments`` table; no sequence (no written table has one); no
function; nothing in ``legacy_archive``; no CREATE on a schema.

Like ``4ab02765a34b`` for the operator CLI, the upgrade first revokes everything the role
holds in ``public`` and ``legacy_archive`` (a host's default privileges or a hand-run GRANT),
then grants back ``RESEARCH_TABLE_PRIVILEGES``.
``tests/integration/test_research_privilege_allowlist.py`` compares the effective privileges
at head with its own copy, and ``test_research_role_behaviour.py`` runs the steps' real
statements logged in as the role.

The role is host configuration (``docs/runbooks/fresh-host-setup.md`` §1a): where it does
not exist this revision grants nothing, and a later weekly run fails to log in.

Forward-only (docs/adr/2026-10-08-forward-only-migrations.md): revoking the grants would
take the weekly report's path away while the role remains.

Revision ID: 3c5e7a9b1d2f
Revises: f836fdee9bf3
"""

from sqlalchemy import text

from alembic import op

revision = "3c5e7a9b1d2f"
down_revision = "f836fdee9bf3"
branch_labels = None
depends_on = None
# Grants only; no table, cursor or event_log content changes.
ledger_contract = "preserved"

_ROLE = "bfx_research"

_R = ("SELECT",)
_UPSERT = ("SELECT", "INSERT", "UPDATE")
_REPLACE = ("SELECT", "INSERT", "DELETE")

RESEARCH_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    # Written: the research tables.
    "funding_stats": _UPSERT,
    "perp_funding_rates": _UPSERT,
    "liquidations": _UPSERT,
    "attribution_weekly": _REPLACE,
    "fill_rate_stats": _REPLACE,
    "fill_rate_model_artifacts": ("SELECT", "INSERT"),
    # Read: attribution / G3 / realized interest (``load_credit_inputs``, ``report_interest``).
    "funding_credit_history": _R,
    "funding_trades": _R,
    "funding_interest_payments": _R,
    "attribution_legacy_offer_cells": _R,
    "venue_credit_mirror": _R,
    "submission_attempt_journal": _R,
    "transport_outcome_journal": _R,
    "execution_resolution_journal": _R,
    # Read: market series of the reports and backtests, the book replay's input.
    "funding_candles": _R,
    "funding_book_snapshots": _R,
}


def _role_exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(text("SELECT 1 FROM pg_roles WHERE rolname=:name"), {"name": name})
        .scalar()
    )


def upgrade() -> None:
    if not _role_exists(_ROLE):
        return
    # Table-level REVOKE also removes the role's column-level grants of that privilege.
    for schema in ("public", "legacy_archive"):
        if not op.get_bind().execute(text("SELECT to_regnamespace(:n) IS NOT NULL"),
                                     {"n": schema}).scalar():
            continue
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {_ROLE}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {_ROLE}")
        op.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {_ROLE}")
        # A host grant of CREATE on the schema would let the role add objects of its own.
        op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_ROLE}")
    for table, privileges in RESEARCH_TABLE_PRIVILEGES.items():
        op.execute(f"GRANT {', '.join(privileges)} ON public.{table} TO {_ROLE}")


def downgrade() -> None:
    raise NotImplementedError("forward-only: docs/adr/2026-10-08-forward-only-migrations.md")
