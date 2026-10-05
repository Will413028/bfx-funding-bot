"""Freeze the legacy authority's tables once the epoch is ``ledger`` (S1-7 H-2 (ii)).

The mirror of ``guard_ledger_authority`` (``f6a7b8c9d0e1``): while the latest
``capital_authority_epoch`` row is ``ledger``, a role other than the table owner cannot
INSERT, UPDATE or DELETE any table below; under ``legacy`` the trigger passes every write, so
this revision changes nothing until the switch. It ships in the switch-capable release, so the
freeze takes effect at the epoch append itself (no migration inside the halt). Owner writes --
migrations, the seed, owner tools -- pass, as for the ledger guard. Data-free: one function and
one statement trigger per table; the schema move and REVOKE wait for S1-8.

Frozen (``FROZEN``): exactly the tables only the legacy authority writes. Each is written by the
event store, the legacy command journal or the legacy capital repository, none of which the
``ledger`` composition builds (``apps/bot_ports._ledger_ports``):

* ``event_log``, ``event_prefix_hashes`` -- the legacy event stream and its prefix chain;
* ``projection_heads``, ``position_state``, ``venue_offer_state``, ``venue_credit_state``,
  ``reconcile_observation`` -- the projections the event-store append updates (UPDATE and
  DELETE included: a projection is rewritten in place);
* ``offer_claims`` -- the legacy offer claims (event store, offer registry);
* ``submission_attempts``, ``execution_uncertainties`` -- the legacy submit journal and its
  uncertainties (the ledger journals in ``submission_attempt_journal`` and quarantines);
* ``capital_snapshot_queries``, ``capital_snapshots`` -- the legacy capital fence and snapshots.

Shared (``SHARED``, never frozen): tables of the execution module that both authorities write,
or that no authority owns, and that must keep working after the switch:

* ``execution_decisions`` -- every decision's audit row; ``submission_attempt_journal`` holds a
  foreign key to it;
* ``trading_state``, ``funding_cancel_all_audit`` -- trading control and kill (authority-neutral);
* ``nav_peak``, ``nav_window_samples`` -- the NAV monitor (published by the ledger cycle too);
* ``diagnostics`` -- runtime diagnostics;
* ``uncertainty_resolution_requests``, ``capital_policy_requests``,
  ``trading_control_requests`` -- the three operator request outboxes (the first has its own
  reverse guard, ``b8c9d0e1f2a4``);
* ``projection_audit.runs`` / ``rows`` / ``receipts`` -- the owner's projection-cutover archive.

Outside the execution module nothing is frozen: ``funding_*`` market and account data, candles,
attribution and report tables, accounts, configuration, deployments and the ledger's own tables
are untouched by this revision.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
"""

from alembic import op

revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

FROZEN: tuple[str, ...] = (
    "event_log",
    "event_prefix_hashes",
    "projection_heads",
    "position_state",
    "venue_offer_state",
    "venue_credit_state",
    "reconcile_observation",
    "offer_claims",
    "submission_attempts",
    "execution_uncertainties",
    "capital_snapshot_queries",
    "capital_snapshots",
)
SHARED: tuple[str, ...] = (
    "execution_decisions",
    "trading_state",
    "funding_cancel_all_audit",
    "nav_peak",
    "nav_window_samples",
    "diagnostics",
    "uncertainty_resolution_requests",
    "capital_policy_requests",
    "trading_control_requests",
    "projection_audit.runs",
    "projection_audit.rows",
    "projection_audit.receipts",
)
_FUNCTION = "guard_legacy_authority"
_TRIGGER = "legacy_authority_write"


def upgrade() -> None:
    # Not SECURITY DEFINER, as ``guard_ledger_authority``: current_user is the writing role,
    # whose epoch SELECT (bfx_bot, bfx_webapi) the read needs.
    op.execute(f"""CREATE FUNCTION public.{_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
          IF current_user IS DISTINCT FROM
               (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID)
             AND (SELECT e.authority FROM public.capital_authority_epoch e
                    ORDER BY e.epoch_seq DESC LIMIT 1) IS NOT DISTINCT FROM 'ledger' THEN
            RAISE EXCEPTION 'legacy write after the authority switch: %', TG_TABLE_NAME;
          END IF;
          RETURN NULL;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_FUNCTION}() FROM PUBLIC")
    for name in FROZEN:
        op.execute(
            f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT OR UPDATE OR DELETE ON public.{name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION public.{_FUNCTION}()"
        )


def downgrade() -> None:
    for name in FROZEN:
        op.execute(f"DROP TRIGGER {_TRIGGER} ON public.{name}")
    op.execute(f"DROP FUNCTION public.{_FUNCTION}()")
