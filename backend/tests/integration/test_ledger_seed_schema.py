"""S1-4c seed schema on clone PostgreSQL: seed origin, evidence bans, nullable seed policy.

Mutation checks (one at a time; revert after each, then rebuild the clone template by running
this file again):

* Drop the ``ledger_seed_writer`` trigger on ``ledger_observation`` (or the role test inside
  ``guard_ledger_seed_writer``): ``test_runtime_role_cannot_write_seed_origin_even_under_ledger_epoch``
  fails (the bot's seed observation succeeds).
* Remove the ``execution_resolution_journal`` / ``uncertainty_resolution_requests`` branch of
  ``guard_ledger_seed_evidence``: ``test_seed_observation_is_never_resolution_or_operator_evidence``
  fails.
* Remove the mirror branches: ``test_seed_observation_is_never_mirror_terminal_evidence`` fails.
* Drop ``ck_submission_attempt_policy_or_seed``: ``test_attempt_policy_may_be_null_only_for_a_seed``
  fails (the unseeded NULL-policy attempt succeeds).
* Drop the ``origin <> 'venue' OR`` arm of ``ck_ledger_observation_acceptance`` (apply the
  coverage rule to seed origin too): ``test_seed_observation_carries_no_history_or_wallet_coverage``
  fails (the valid seed observation is rejected).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from bfx_funding_bot.modules.ledger.tables import LedgerObservationRow, SubmissionAttemptJournalRow
from tests.pg_templates import alembic

from .test_ledger_schema_roles import (
    _A,
    _B,
    _D2,
    _H,
    _HC,
    _O,
    _P,
    _Q,
    _basis_sql,
    _build,
    _member_sql,
    _observation_sql,
    _query_sql,
    _seed,
    pre_switch_url,
)

pytestmark = pytest.mark.integration

_PREVIOUS = "b5c6d7e8f9a0"


@pytest.fixture
def ledger_db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("ledger_s1_roles", _build))
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def seeded(ledger_db):
    with ledger_db.begin() as conn:
        _seed(conn)
    return ledger_db


def _seed_observation_sql(
    observation_id: str,
    query_id: str,
    *,
    accepted: bool = True,
    wallets: bool = False,
    history: bool = False,
    trades: bool = False,
    pages: str = "NULL",
    origin: str = "legacy_seed",
    overrides: dict[str, str] | None = None,
) -> str:
    """A seed header; ``overrides`` replaces or adds single columns (SQL literals)."""

    def flag(value: bool) -> str:
        return str(value).lower()

    columns = {
        "id": f"'{observation_id}'",
        "query_id": f"'{query_id}'",
        "exchange_account_id": f"'{_A}'",
        "deployment_environment": "'ci'",
        "schema_version": "1",
        "query_finished_at_ms": "2",
        "confirmation_finished_at_ms": "3",
        "accept_revision": "0",
        "origin": f"'{origin}'",
        "wallets_complete": flag(wallets),
        "offers_complete": "true",
        "credits_complete": "true",
        "loans_complete": "true",
        "offer_history_complete": flag(history),
        "credit_history_complete": flag(history),
        "trades_complete": flag(trades),
        "offer_history_pages": pages,
        "first_digest": "'digest'",
        "confirmation_digest": "'digest'",
        "accepted": flag(accepted),
        "evidence": "'{}'",
    }
    columns.update(overrides or {})
    return (
        f"INSERT INTO ledger_observation ({', '.join(columns)}) "
        f"VALUES ({', '.join(columns.values())})"
    )


def _new_query(conn, revision: int) -> str:
    query_id = str(uuid4())
    conn.exec_driver_sql(_query_sql(query_id, revision))
    return query_id


def _owner_seed_observation(conn, revision: int = 2) -> str:
    """The owner writes a seed observation on a fresh newest query."""
    observation_id = str(uuid4())
    conn.exec_driver_sql(_seed_observation_sql(observation_id, _new_query(conn, revision)))
    return observation_id


def _append_epoch(conn, authority: str) -> None:
    """The owner appends the next epoch (a database at head already starts on ``ledger``:
    the genesis epoch; appending it again keeps the precondition explicit)."""
    conn.exec_driver_sql(
        "INSERT INTO capital_authority_epoch (epoch_seq, authority, set_at_ms, actor, reason) "
        f"SELECT max(epoch_seq) + 1, '{authority}', max(epoch_seq) + 1, 'test', 'switch' "
        "FROM capital_authority_epoch"
    )


def _decision_sql(decision_id: str) -> str:
    return f"""INSERT INTO execution_decisions(decision_id, account_id, exchange_account_id,
      deployment_environment, reconcile_id, cell_id, symbol, signal_correlation_id, outcome,
      signal_rate, amount_usdt, duration_days, model_evidence, safety_result, execution_policy,
      service_version, config_hash, occurred_at_ms, recorded_at_ms)
      VALUES ('{decision_id}', 'account', '{_A}', 'ci', 'r', 'cell', 'fUST', '{decision_id}',
      'submitted', 0, 1, 2, '{{}}', '{{}}', 'policy', 'test', 'hash', 1, 1)"""


def _insert_attempt(conn, seq: int, *, policy: str | None, provenance: str | None) -> None:
    """One attempt on its own decision (the decision id is unique per attempt)."""
    decision = f"decision-{uuid4()}"
    conn.exec_driver_sql(_decision_sql(decision))
    conn.exec_driver_sql(
        _attempt_sql(str(uuid4()), seq, policy=policy, provenance=provenance, decision=decision)
    )


def _attempt_sql(
    attempt_id: str,
    seq: int,
    *,
    policy: str | None,
    provenance: str | None,
    decision: str = _D2,
) -> str:
    policy_value = "NULL" if policy is None else f"'{policy}'"
    provenance_value = "NULL" if provenance is None else f"'{provenance}'"
    return f"""INSERT INTO submission_attempt_journal(attempt_id, execution_decision_id,
      exchange_account_id, deployment_environment, symbol, cell_id, attempt_seq,
      normalized_payload, payload_sha256, basis_id, policy_revision_id,
      authorization_evidence, seed_provenance, started_at_ms)
      VALUES ('{attempt_id}', '{decision}', '{_A}', 'ci', 'fUST', 'cell', {seq}, '{{"amount": "1"}}',
      'hash', '{_B}', {policy_value}, '{{}}', {provenance_value}, 4)"""


def _quarantine_sql(quarantine_id: str) -> str:
    return f"""INSERT INTO quarantine_opening(quarantine_id, exchange_account_id,
      deployment_environment, symbol, intended_amount, opened_at_ms, opened_revision, evidence)
      VALUES ('{quarantine_id}', '{_A}', 'ci', 'fUST', 1, 4, 1, '{{}}')"""


def _resolution_sql(quarantine_id: str, observation_id: str) -> str:
    return f"""INSERT INTO execution_resolution_journal(id, quarantine_id, exchange_account_id,
      deployment_environment, symbol, action, venue_offer_id, observation_id, actor_kind,
      actor_id, resolved_at_ms, reason, evidence)
      VALUES ('{uuid4()}', '{quarantine_id}', '{_A}', 'ci', 'fUST', 'bound_to_venue', 'offer-1',
      '{observation_id}', 'operator', 'test', 5, 'test', '{{}}')"""


def _request_sql(observation_id: str) -> str:
    return f"""INSERT INTO uncertainty_resolution_requests(request_id, exchange_account_id,
      deployment_environment, uncertainty_id, action, observation_id, requested_by, created_at_ms)
      VALUES ('{uuid4()}', '{_A}', 'ci', '{uuid4()}', 'mark_not_accepted', '{observation_id}',
      'operator', 6)"""


def _seed_history_sql(
    table: str, history_id: str, observation_id: str, venue_id: str | None = None
) -> str:
    if table == "ledger_observation_offer_history":
        return f"""INSERT INTO ledger_observation_offer_history(id, observation_id,
          venue_offer_id, symbol, amount_original, amount_remaining, rate_observed, status,
          mts_created, terminal_kind, occurred_at_ms, raw)
          VALUES ('{history_id}', '{observation_id}', '{venue_id or 'offer-1'}', 'fUST', 1, 0, true,
          'CANCELLED', 1, 'cancelled', 3, '{{}}')"""
    return f"""INSERT INTO ledger_observation_credit_history(id, observation_id, venue_credit_id,
      source_kind, symbol, amount, status, terminal_kind, occurred_at_ms, raw)
      VALUES ('{history_id}', '{observation_id}', '{venue_id or 'credit-1'}', 'credit', 'fUST', 1, 'CLOSED',
      'closed', 3, '{{}}')"""


def test_default_origin_is_venue_and_venue_still_needs_every_coverage_flag(seeded) -> None:
    with seeded.begin() as conn:
        assert conn.scalar(text(f"SELECT origin FROM ledger_observation WHERE id='{_O}'")) == "venue"
    for revision, kwargs in enumerate(({"complete": False}, {"trades_complete": False}), start=2):
        with seeded.begin() as conn:
            query_id = _new_query(conn, revision)
        with seeded.begin() as conn, pytest.raises(Exception, match="ck_ledger_observation_acceptance"):
            conn.exec_driver_sql(_observation_sql(str(uuid4()), query_id, accepted=True, **kwargs))
        # An explicit venue origin is no exemption either.
        with seeded.begin() as conn, pytest.raises(Exception, match="ck_ledger_observation_acceptance"):
            conn.exec_driver_sql(
                _seed_observation_sql(
                    str(uuid4()), query_id, origin="venue", wallets=False, history=True
                )
            )


def test_origin_is_one_of_the_two_values(seeded) -> None:
    with seeded.begin() as conn:
        query_id = _new_query(conn, 2)
    with seeded.begin() as conn, pytest.raises(Exception, match="ck_ledger_observation_origin"):
        conn.exec_driver_sql(_seed_observation_sql(str(uuid4()), query_id, origin="other"))


_SEED_CLAUSE_CASES = {
    "not_accepted": {"accepted": False},
    "wallets": {"wallets": True},
    "offer_history_flag": {"overrides": {"offer_history_complete": "true"}},
    "credit_history_flag": {"overrides": {"credit_history_complete": "true"}},
    "trades_flag": {"trades": True},
    "trades_range": {
        "overrides": {"trades_requested_start_ms": "1", "trades_requested_end_ms": "2"}
    },
    "history_range": {
        "overrides": {"history_requested_start_ms": "1", "history_requested_end_ms": "2"}
    },
    "history_bounds": {
        "overrides": {"history_oldest_mts_created": "1", "history_newest_mts_created": "2"}
    },
    "offer_history_pages": {"pages": "1"},
    "credit_history_pages": {"overrides": {"credit_history_pages": "1"}},
}


@pytest.mark.parametrize("case", list(_SEED_CLAUSE_CASES))
def test_seed_observation_clause_is_enforced_alone(seeded, case: str) -> None:
    with seeded.begin() as conn:
        query_id = _new_query(conn, 2)
    with seeded.begin() as conn, pytest.raises(Exception, match="ck_ledger_observation_seed"):
        conn.exec_driver_sql(
            _seed_observation_sql(str(uuid4()), query_id, **_SEED_CLAUSE_CASES[case])
        )
    with seeded.begin() as conn:  # the truthful seed shape is accepted
        conn.exec_driver_sql(_seed_observation_sql(str(uuid4()), query_id))


def test_runtime_role_cannot_write_seed_origin_even_under_ledger_epoch(seeded) -> None:
    with seeded.begin() as conn:
        _append_epoch(conn, "ledger")
        venue_query = _new_query(conn, 2)
        seed_query = _new_query(conn, 3)
    with seeded.begin() as conn:  # control: the bot writes a complete venue observation
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(_observation_sql(str(uuid4()), venue_query, accepted=False))
    with seeded.begin() as conn, pytest.raises(Exception, match="owner only"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(_seed_observation_sql(str(uuid4()), seed_query))
    with seeded.begin() as conn:  # the owner writes it
        conn.exec_driver_sql(_seed_observation_sql(str(uuid4()), seed_query))
        assert conn.scalar(text("SELECT count(*) FROM ledger_observation WHERE origin='legacy_seed'")) == 1


def test_seed_observation_is_never_resolution_or_operator_evidence(seeded) -> None:
    with seeded.begin() as conn:
        seed = _owner_seed_observation(conn)
        venue_quarantine, seed_quarantine = str(uuid4()), str(uuid4())
        conn.exec_driver_sql(_quarantine_sql(venue_quarantine))
        conn.exec_driver_sql(_quarantine_sql(seed_quarantine))
        conn.exec_driver_sql(_resolution_sql(venue_quarantine, _O))  # control
    with seeded.begin() as conn, pytest.raises(Exception, match="a seed observation is not evidence"):
        conn.exec_driver_sql(_resolution_sql(seed_quarantine, seed))
    with seeded.begin() as conn:  # control: venue evidence on a request
        conn.exec_driver_sql(_request_sql(_O))
    with seeded.begin() as conn, pytest.raises(Exception, match="a seed observation is not evidence"):
        conn.exec_driver_sql(_request_sql(seed))
    # The web API cannot read ``origin``; the rule still holds for its INSERT (definer rights).
    with seeded.begin() as conn:
        _append_epoch(conn, "ledger")
    with seeded.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(_request_sql(_O))
    with seeded.begin() as conn, pytest.raises(Exception, match="a seed observation is not evidence"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql(_request_sql(seed))


@pytest.mark.parametrize(
    ("mirror", "history_table", "kind"),
    [
        ("venue_offer_mirror", "ledger_observation_offer_history", "cancelled"),
        ("venue_credit_mirror", "ledger_observation_credit_history", "closed"),
    ],
)
def test_seed_observation_is_never_mirror_terminal_evidence(
    seeded, mirror: str, history_table: str, kind: str
) -> None:
    history_id = str(uuid4())
    with seeded.begin() as conn:
        seed = _owner_seed_observation(conn)
        conn.exec_driver_sql(_seed_history_sql(history_table, history_id, seed))
    with seeded.begin() as conn, pytest.raises(Exception, match="a seed observation is not evidence"):
        conn.exec_driver_sql(
            f"UPDATE {mirror} SET present_in_latest_accepted_snapshot=false, "
            f"terminal_evidence_id='{history_id}', terminal_kind='{kind}'"
        )
    # Control: the venue history row of the same object is accepted as terminal evidence.
    evidence = _H if mirror == "venue_offer_mirror" else _HC
    with seeded.begin() as conn:
        conn.exec_driver_sql(
            f"UPDATE {mirror} SET present_in_latest_accepted_snapshot=false, "
            f"terminal_evidence_id='{evidence}', terminal_kind='{kind}'"
        )


def _terminal_mirror_insert_sql(mirror: str, venue_id: str, history_id: str) -> str:
    if mirror == "venue_offer_mirror":
        return f"""INSERT INTO venue_offer_mirror(exchange_account_id, deployment_environment,
          venue_offer_id, symbol, amount_original, amount_remaining, rate_observed, status,
          mts_created, last_accepted_observation_id, present_in_latest_accepted_snapshot,
          terminal_evidence_id, terminal_kind)
          VALUES ('{_A}', 'ci', '{venue_id}', 'fUST', 1, 0, true, 'CANCELLED', 1, '{_O}', false,
          '{history_id}', 'cancelled')"""
    return f"""INSERT INTO venue_credit_mirror(exchange_account_id, deployment_environment,
      venue_credit_id, source_kind, symbol, amount, status, last_accepted_observation_id,
      present_in_latest_accepted_snapshot, terminal_evidence_id, terminal_kind)
      VALUES ('{_A}', 'ci', '{venue_id}', 'credit', 'fUST', 1, 'CLOSED', '{_O}', false,
      '{history_id}', 'closed')"""


@pytest.mark.parametrize(
    ("mirror", "history_table"),
    [
        ("venue_offer_mirror", "ledger_observation_offer_history"),
        ("venue_credit_mirror", "ledger_observation_credit_history"),
    ],
)
def test_seed_observation_is_never_mirror_terminal_evidence_on_insert(
    seeded, mirror: str, history_table: str
) -> None:
    seed_history, venue_history = str(uuid4()), str(uuid4())
    with seeded.begin() as conn:
        seed = _owner_seed_observation(conn)
        conn.exec_driver_sql(_seed_history_sql(history_table, seed_history, seed, "obj-seed"))
        conn.exec_driver_sql(_seed_history_sql(history_table, venue_history, _O, "obj-venue"))
    with seeded.begin() as conn, pytest.raises(Exception, match="a seed observation is not evidence"):
        conn.exec_driver_sql(_terminal_mirror_insert_sql(mirror, "obj-seed", seed_history))
    # Control: the same insert citing a venue history row is accepted.
    with seeded.begin() as conn:
        conn.exec_driver_sql(_terminal_mirror_insert_sql(mirror, "obj-venue", venue_history))


def test_seed_observation_is_allowed_as_member_mirror_anchor_and_basis(seeded) -> None:
    seed, offer_id = str(uuid4()), str(uuid4())
    with seeded.begin() as conn:
        query_id = _new_query(conn, 2)
        conn.exec_driver_sql(_seed_observation_sql(seed, query_id))
        conn.exec_driver_sql(
            f"""INSERT INTO ledger_observation_offer (id, observation_id, venue_offer_id, symbol,
            amount_original, amount_remaining, rate_observed, status, mts_created, raw)
            VALUES ('{offer_id}', '{seed}', 'seed-offer', 'fUST', 1, 1, true, 'ACTIVE', 1, '{{}}')"""
        )
        conn.exec_driver_sql(_member_sql("offer", "seed-offer", seed))
        conn.exec_driver_sql(
            f"UPDATE venue_offer_mirror SET last_accepted_observation_id='{seed}'"
        )
        conn.exec_driver_sql(_basis_sql(str(uuid4()), seed))
        assert (
            conn.scalar(
                text(
                    "SELECT count(*) FROM quarantine_member "
                    f"WHERE quarantine_id='{_Q}' AND observation_id='{seed}'"
                )
            )
            == 1
        )


def test_attempt_policy_may_be_null_only_for_a_seed(seeded) -> None:
    with seeded.begin() as conn, pytest.raises(
        Exception, match="ck_submission_attempt_policy_or_seed"
    ):
        _insert_attempt(conn, 5, policy=None, provenance=None)
    with seeded.begin() as conn:  # a seeded attempt names no policy
        _insert_attempt(conn, 6, policy=None, provenance="{}")
    with seeded.begin() as conn:  # a seeded attempt may still name one
        _insert_attempt(conn, 7, policy=_P, provenance="{}")
    with seeded.begin() as conn:  # an ordinary attempt names one
        _insert_attempt(conn, 8, policy=_P, provenance=None)


def test_runtime_role_cannot_write_a_policyless_attempt(seeded) -> None:
    with seeded.begin() as conn:
        _append_epoch(conn, "ledger")
    with seeded.begin() as conn:  # control: the bot writes an ordinary attempt
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        _insert_attempt(conn, 5, policy=_P, provenance=None)
    with seeded.begin() as conn, pytest.raises(Exception, match="owner only"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        _insert_attempt(conn, 6, policy=None, provenance="{}")


def test_origin_is_not_granted_beyond_the_bot(seeded) -> None:
    """The web roles never read ``origin`` (the closure verifier's cutover reader, which did from
    d7e8f9a0b1c2 on, is retired in d3e4f5a6b7c8)."""
    with seeded.connect() as conn:
        for role in ("bfx_webapi", "bfx_webauth"):
            assert not conn.scalar(
                text("SELECT has_column_privilege(:r,'ledger_observation','origin','SELECT')"),
                {"r": role},
            )
        assert conn.scalar(
            text("SELECT has_column_privilege('bfx_bot','ledger_observation','origin','INSERT')")
        )


def test_orm_matches_the_new_shape() -> None:
    origin = LedgerObservationRow.__table__.c.origin
    policy = SubmissionAttemptJournalRow.__table__.c.policy_revision_id
    assert (origin.nullable, str(origin.server_default.arg)) == (False, "'venue'")
    assert policy.nullable is True


def test_migration_round_trip_and_populated_downgrade(seeded) -> None:
    url = seeded.url.render_as_string(hide_password=False)
    seeded.dispose()
    pre_switch_url(url)  # the downgrade below the genesis starts pre-switch
    alembic(url, "downgrade", _PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert not conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                    "table_name='ledger_observation' AND column_name='origin')"
                )
            )
            assert not conn.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname LIKE 'guard_ledger_seed%')")
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns WHERE "
                        "table_name='submission_attempt_journal' AND column_name='policy_revision_id'"
                    )
                )
                == "NO"
            )
    finally:
        engine.dispose()
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:  # existing rows become venue rows
            assert conn.scalar(text("SELECT count(*) FROM ledger_observation WHERE origin='venue'")) == 1
    finally:
        engine.dispose()


def test_downgrade_refuses_seed_rows(seeded) -> None:
    with seeded.begin() as conn:
        _owner_seed_observation(conn)
    url = seeded.url.render_as_string(hide_password=False)
    seeded.dispose()
    pre_switch_url(url)  # the downgrade below the genesis starts pre-switch
    with pytest.raises(Exception, match="refuse downgrade with seed observations"):
        alembic(url, "downgrade", _PREVIOUS)


def test_downgrade_refuses_policyless_attempts(seeded) -> None:
    with seeded.begin() as conn:
        _insert_attempt(conn, 6, policy=None, provenance="{}")
    url = seeded.url.render_as_string(hide_password=False)
    seeded.dispose()
    pre_switch_url(url)  # the downgrade below the genesis starts pre-switch
    with pytest.raises(Exception, match="refuse downgrade with seeded attempts"):
        alembic(url, "downgrade", _PREVIOUS)
