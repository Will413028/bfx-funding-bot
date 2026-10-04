"""Schema for the one-time legacy closure seed (S1-4c): seed origin, nullable seed policy.

Schema only. No seed writer exists yet and no runtime path changes: every existing and every
runtime-written row is ``origin='venue'`` with a policy revision, exactly as before.

``ledger_observation.origin`` (``venue`` | ``legacy_seed``, default ``venue``). A venue
observation keeps the acceptance rule unchanged (all seven coverage flags when accepted). A
``legacy_seed`` observation is built from the final legacy accepted snapshot and must be
truthful about what that snapshot is not. Its constraint set (``ck_ledger_observation_seed``):

* ``accepted`` is true (a seed that is not accepted can anchor nothing);
* ``wallets_complete`` is false (the snapshot has ``wallet_available`` only, no balance or
  wallet type);
* ``offer_history_complete``, ``credit_history_complete`` and ``trades_complete`` are false and
  no history range, history bound, page count or trade range is recorded (the snapshot has no
  history and no trades, so none may be claimed).

``offers_complete``, ``credits_complete`` and ``loans_complete`` are left to the writer: the
snapshot does cover live offers, credits and loans. The seed's ``first_digest`` and
``confirmation_digest`` must still match (existing check).

Runtime roles never write seed rows (DB-enforced, same owner exemption as
``guard_ledger_authority``, ``f6a7b8c9d0e1``): a row trigger rejects an ``origin='legacy_seed'``
observation, and a submission attempt with a NULL policy revision, unless ``current_user`` owns
the table. The epoch dormancy trigger does not cover this (it passes every role once the epoch
is ``ledger``).

A seed observation is never evidence of a resolution. A SECURITY DEFINER trigger function (pinned
``search_path``, trigger return type, so the cutover guard's SECURITY DEFINER scan excludes it;
needed because ``bfx_webapi`` cannot read ``origin``) rejects a seed observation as
``execution_resolution_journal.observation_id``, as ``uncertainty_resolution_requests
.observation_id`` (operator evidence) and as the observation behind a mirror's
``terminal_evidence_id`` (offer or credit history row). Deliberately allowed for a seed
observation: ``quarantine_member.observation_id`` (the member of a seeded quarantine can cite no
other observation at seed time), the mirrors' ``last_accepted_observation_id``, and the
``accepted_capital_basis`` observation (the seed basis).

``submission_attempt_journal.policy_revision_id`` becomes nullable under
``ck_submission_attempt_policy_or_seed``: ``policy_revision_id IS NOT NULL OR seed_provenance IS
NOT NULL`` (the legacy authorizing revision was never stored, so a seeded attempt cannot name
one). A seeded attempt may still name a policy; only the owner may write a NULL one.

Grants: ``origin`` is granted to no role beyond bfx_bot's table-level INSERT/SELECT. The
explicit-column capital reader and the cutover comparison read named columns and never
``origin``; a later verifier that must tell seed from venue adds the one column grant then.

Data-free. Prod precheck: every ledger table has 0 rows (the ledger is dormant under the
``legacy`` epoch). The downgrade refuses when a seed observation or a NULL-policy attempt exists.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
"""

from sqlalchemy import text

from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None
ledger_contract = "preserved"

_OBSERVATION = "ledger_observation"
_ATTEMPT = "submission_attempt_journal"
_WRITER_FUNCTION = "guard_ledger_seed_writer"
_WRITER_TRIGGER = "ledger_seed_writer"
_EVIDENCE_FUNCTION = "guard_ledger_seed_evidence"
_EVIDENCE_TRIGGER = "ledger_seed_evidence"
_EVIDENCE_TABLES = (
    ("execution_resolution_journal", "BEFORE INSERT"),
    ("uncertainty_resolution_requests", "BEFORE INSERT"),
    ("venue_offer_mirror", "BEFORE INSERT OR UPDATE"),
    ("venue_credit_mirror", "BEFORE INSERT OR UPDATE"),
)
_COVERAGE = (
    "wallets_complete AND offers_complete AND credits_complete AND loans_complete AND "
    "offer_history_complete AND credit_history_complete AND trades_complete"
)
_ACCEPTANCE = f"NOT accepted OR origin <> 'venue' OR ({_COVERAGE})"
_ACCEPTANCE_PREVIOUS = f"NOT accepted OR ({_COVERAGE})"
_SEED = (
    "origin <> 'legacy_seed' OR (accepted AND NOT wallets_complete AND "
    "NOT offer_history_complete AND NOT credit_history_complete AND NOT trades_complete AND "
    "trades_requested_start_ms IS NULL AND history_requested_start_ms IS NULL AND "
    "history_oldest_mts_created IS NULL AND offer_history_pages IS NULL AND "
    "credit_history_pages IS NULL)"
)


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE public.{_OBSERVATION} ADD COLUMN origin text NOT NULL DEFAULT 'venue'"
    )
    op.execute(
        f"ALTER TABLE public.{_OBSERVATION} ADD CONSTRAINT ck_ledger_observation_origin "
        "CHECK (origin IN ('venue', 'legacy_seed'))"
    )
    op.execute(
        f"ALTER TABLE public.{_OBSERVATION} DROP CONSTRAINT ck_ledger_observation_acceptance"
    )
    op.execute(
        f"ALTER TABLE public.{_OBSERVATION} ADD CONSTRAINT ck_ledger_observation_acceptance "
        f"CHECK ({_ACCEPTANCE})"
    )
    op.execute(
        f"ALTER TABLE public.{_OBSERVATION} ADD CONSTRAINT ck_ledger_observation_seed "
        f"CHECK ({_SEED})"
    )

    op.execute(f"ALTER TABLE public.{_ATTEMPT} ALTER COLUMN policy_revision_id DROP NOT NULL")
    op.execute(
        f"ALTER TABLE public.{_ATTEMPT} ADD CONSTRAINT ck_submission_attempt_policy_or_seed "
        "CHECK (policy_revision_id IS NOT NULL OR seed_provenance IS NOT NULL)"
    )

    # Invoker rights: current_user is the writing role, as in guard_ledger_authority.
    op.execute(f"""CREATE FUNCTION public.{_WRITER_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE seed_row boolean;
        BEGIN
          IF TG_TABLE_NAME = '{_OBSERVATION}' THEN
            seed_row := NEW.origin = 'legacy_seed';
          ELSE
            seed_row := NEW.policy_revision_id IS NULL;
          END IF;
          IF seed_row AND current_user IS DISTINCT FROM
               (SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID) THEN
            RAISE EXCEPTION 'ledger seed rows are written by the table owner only: %',
              TG_TABLE_NAME;
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_WRITER_FUNCTION}() FROM PUBLIC")
    for name in (_OBSERVATION, _ATTEMPT):
        op.execute(
            f"CREATE TRIGGER {_WRITER_TRIGGER} BEFORE INSERT ON public.{name} "
            f"FOR EACH ROW EXECUTE FUNCTION public.{_WRITER_FUNCTION}()"
        )

    # SECURITY DEFINER: the web API writing a request cannot read ledger_observation.origin.
    op.execute(f"""CREATE FUNCTION public.{_EVIDENCE_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE seed_evidence boolean;
        BEGIN
          IF TG_TABLE_NAME IN ('execution_resolution_journal',
                               'uncertainty_resolution_requests') THEN
            seed_evidence := NEW.observation_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM public.ledger_observation o
              WHERE o.id = NEW.observation_id AND o.origin = 'legacy_seed');
          ELSIF TG_TABLE_NAME = 'venue_offer_mirror' THEN
            seed_evidence := NEW.terminal_evidence_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM public.ledger_observation_offer_history h
              JOIN public.ledger_observation o ON o.id = h.observation_id
              WHERE h.id = NEW.terminal_evidence_id AND o.origin = 'legacy_seed');
          ELSE
            seed_evidence := NEW.terminal_evidence_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM public.ledger_observation_credit_history h
              JOIN public.ledger_observation o ON o.id = h.observation_id
              WHERE h.id = NEW.terminal_evidence_id AND o.origin = 'legacy_seed');
          END IF;
          IF seed_evidence THEN
            RAISE EXCEPTION 'a seed observation is not evidence: %', TG_TABLE_NAME;
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(f"REVOKE ALL ON FUNCTION public.{_EVIDENCE_FUNCTION}() FROM PUBLIC")
    for name, when in _EVIDENCE_TABLES:
        op.execute(
            f"CREATE TRIGGER {_EVIDENCE_TRIGGER} {when} ON public.{name} "
            f"FOR EACH ROW EXECUTE FUNCTION public.{_EVIDENCE_FUNCTION}()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        text(f"SELECT EXISTS (SELECT 1 FROM public.{_OBSERVATION} WHERE origin <> 'venue')")
    ).scalar():
        raise RuntimeError(f"refuse downgrade with seed observations: {_OBSERVATION}")
    if bind.execute(
        text(f"SELECT EXISTS (SELECT 1 FROM public.{_ATTEMPT} WHERE policy_revision_id IS NULL)")
    ).scalar():
        raise RuntimeError(f"refuse downgrade with seeded attempts lacking a policy: {_ATTEMPT}")
    for name, _ in _EVIDENCE_TABLES:
        op.execute(f"DROP TRIGGER {_EVIDENCE_TRIGGER} ON public.{name}")
    op.execute(f"DROP FUNCTION public.{_EVIDENCE_FUNCTION}()")
    for name in (_OBSERVATION, _ATTEMPT):
        op.execute(f"DROP TRIGGER {_WRITER_TRIGGER} ON public.{name}")
    op.execute(f"DROP FUNCTION public.{_WRITER_FUNCTION}()")
    op.execute(f"ALTER TABLE public.{_ATTEMPT} DROP CONSTRAINT ck_submission_attempt_policy_or_seed")
    op.execute(f"ALTER TABLE public.{_ATTEMPT} ALTER COLUMN policy_revision_id SET NOT NULL")
    op.execute(f"ALTER TABLE public.{_OBSERVATION} DROP CONSTRAINT ck_ledger_observation_seed")
    op.execute(
        f"ALTER TABLE public.{_OBSERVATION} DROP CONSTRAINT ck_ledger_observation_acceptance"
    )
    op.execute(
        f"ALTER TABLE public.{_OBSERVATION} ADD CONSTRAINT ck_ledger_observation_acceptance "
        f"CHECK ({_ACCEPTANCE_PREVIOUS})"
    )
    op.execute(f"ALTER TABLE public.{_OBSERVATION} DROP CONSTRAINT ck_ledger_observation_origin")
    op.execute(f"ALTER TABLE public.{_OBSERVATION} DROP COLUMN origin")
