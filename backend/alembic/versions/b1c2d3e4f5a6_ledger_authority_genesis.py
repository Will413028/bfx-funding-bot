"""Start every database without legacy history on the ledger authority (S1-8 D2 genesis).

``f6a7b8c9d0e1`` seeds ``capital_authority_epoch`` with ``legacy``; only the S1-7 switch
appended ``ledger``. This build runs the ledger alone, so a database whose latest epoch is
still ``legacy`` is decided here, once, by what it holds:

* latest epoch ``ledger`` (a switched database, prod): nothing to do;
* latest ``legacy`` and an empty ``event_log`` (a fresh host, CI): append a ``ledger`` epoch.
  Nothing was ever lent under the legacy authority, so there is nothing to seed; offers
  already resting at the venue are foreign to the ledger, which is what they are;
* latest ``legacy`` and a non-empty ``event_log``: refuse. That capital is known to the
  ledger only through the seed and the S1-7 switch, which run against the legacy runtime of
  the previous release: finish the switch there, then upgrade.

Downgrade takes back only what upgrade wrote, the genesis row, while it is still the latest
epoch. Nothing may have run on the ledger since (``LEDGER_WRITTEN``: no observation, attempt,
quarantine, resolution, basis, clock or mirror row); otherwise the downgrade refuses, since an
older build reading ``legacy`` again would disown that ledger. The epoch table is insert-only
for every role; the owner lifts its immutability trigger for that one statement, inside the
migration's transaction. A later latest epoch (a switched database) keeps every row; its
epochs are its record, and ``f6a7b8c9d0e1`` refuses below that.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
"""

from sqlalchemy import text

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

ACTOR = f"migration {revision} genesis"
REASON = "genesis: no legacy history"


def upgrade() -> None:
    bind = op.get_bind()
    latest = bind.execute(text(
        "SELECT epoch_seq, authority FROM public.capital_authority_epoch "
        "ORDER BY epoch_seq DESC LIMIT 1")).first()
    if latest is None:
        raise RuntimeError("capital_authority_epoch has no row; f6a7b8c9d0e1 seeds one")
    if latest.authority == "ledger":
        return
    if bind.execute(text("SELECT EXISTS (SELECT 1 FROM public.event_log)")).scalar():
        seeded = bind.execute(text(
            "SELECT EXISTS (SELECT 1 FROM public.ledger_observation "
            "WHERE origin = 'legacy_seed')")).scalar()
        state = ("seeded but never switched" if seeded
                 else "without a legacy_seed observation")
        raise RuntimeError(
            f"refuse ledger genesis: the latest capital authority epoch is legacy and "
            f"event_log holds legacy history {state}; finish the S1-7 ledger switch on the "
            f"previous release, then upgrade")
    bind.execute(
        text(
            "INSERT INTO public.capital_authority_epoch "
            "(epoch_seq, authority, set_at_ms, actor, reason, evidence) "
            "VALUES (:seq, 'ledger', (extract(epoch FROM clock_timestamp()) * 1000)::bigint, "
            ":actor, :reason, NULL)"
        ),
        {"seq": latest.epoch_seq + 1, "actor": ACTOR, "reason": REASON},
    )


# A row in any of these means something ran on the ledger since the genesis epoch.
LEDGER_WRITTEN = (
    "ledger_observation_query", "ledger_observation", "submission_attempt_journal",
    "transport_outcome_journal", "quarantine_opening", "execution_resolution_journal",
    "accepted_capital_basis", "capital_command_clock", "venue_offer_mirror",
    "venue_credit_mirror",
)


def downgrade() -> None:
    bind = op.get_bind()
    latest = bind.execute(text(
        "SELECT epoch_seq, authority, actor FROM public.capital_authority_epoch "
        "ORDER BY epoch_seq DESC LIMIT 1")).first()
    if latest is None or latest.actor != ACTOR:
        return
    written = [table for table in LEDGER_WRITTEN if bind.execute(
        text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")).scalar()]
    if written:
        raise RuntimeError(
            f"refuse downgrade of ledger genesis: the ledger was written since "
            f"({', '.join(written)})")
    op.execute("ALTER TABLE public.capital_authority_epoch DISABLE TRIGGER immutable_ledger_write")
    bind.execute(text("DELETE FROM public.capital_authority_epoch WHERE epoch_seq = :seq"),
                 {"seq": latest.epoch_seq})
    op.execute("ALTER TABLE public.capital_authority_epoch ENABLE TRIGGER immutable_ledger_write")
