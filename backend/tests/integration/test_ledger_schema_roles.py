"""Ledger S1-1 on clone PostgreSQL, including production-like default grants.

Mutation checks (one at a time; revert after each, then run this file on a new
clone):

* Skip ``_revoke_defaults('bfx_bot')`` in the migration role loop. The ACL
  assertion for immutable-table UPDATE fails under simulated default grants.
* Skip ``immutable_ledger_write`` creation when ``name == 'ledger_observation'``.
  The owner UPDATE and DELETE assertions fail.
* Remove ``uq_execution_resolution_quarantine`` from the frozen migration DDL
  after the autogenerate splice. The second quarantine resolution succeeds.
* Skip ``guard_ledger_scope_insert`` creation when
  ``name == 'submission_attempt_journal'``. The mismatched decision INSERT
  succeeds (its FK still points to a real decision).
* Drop ``guard_ledger_observation_accept_insert``. Stale and revision-mismatched
  accepted observations succeed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text

from bfx_funding_bot.modules.ledger.tables import LEDGER_TABLES
from tests.pg_templates import alembic

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_A = "00000000-0000-0000-0000-00000000a001"
_O = "00000000-0000-0000-0000-00000000a002"
_QID = "00000000-0000-0000-0000-00000000a011"
_B = "00000000-0000-0000-0000-00000000a003"
_P = "00000000-0000-0000-0000-00000000a004"
_D = "ledger-decision"
_D2 = "ledger-decision-unspent"
_T = "00000000-0000-0000-0000-00000000a005"
_Q = "00000000-0000-0000-0000-00000000a006"
_R = "00000000-0000-0000-0000-00000000a007"
_H = "00000000-0000-0000-0000-00000000a008"
_HC = "00000000-0000-0000-0000-00000000a009"
_C = "00000000-0000-0000-0000-00000000a010"


def _build(url: str) -> None:
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DO $$ BEGIN IF NOT EXISTS "
            "(SELECT FROM pg_roles WHERE rolname='bfx_webauth') "
            "THEN CREATE ROLE bfx_webauth; END IF; END $$"
        )
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_webauth")
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_webauth"
        )
    engine.dispose()
    alembic(url, "upgrade", "head")
    alembic(url, "check")


def _observation_sql(
    observation_id: str,
    query_id: str,
    *,
    accepted: bool,
    first: str = "digest",
    confirmation: str = "digest",
    accept_revision: int | None = None,
    complete: bool = True,
    finished_at_ms: int = 2,
) -> str:
    state = str(accepted).lower()
    coverage = str(complete).lower()
    accepted_revision = 0 if accept_revision is None else accept_revision
    return f"""INSERT INTO ledger_observation (
      id, query_id, exchange_account_id, deployment_environment, schema_version,
      query_finished_at_ms, confirmation_finished_at_ms,
      accept_revision, wallets_complete, offers_complete,
      credits_complete, loans_complete, offer_history_complete, credit_history_complete,
      first_digest, confirmation_digest, accepted, evidence)
      VALUES ('{observation_id}', '{query_id}', '{_A}', 'ci', 1,
      {finished_at_ms}, {finished_at_ms + 1}, {accepted_revision}, {coverage}, {coverage},
      {coverage}, {coverage}, {coverage}, {coverage},
      '{first}', '{confirmation}', {state}, '{{}}')"""


def _offer_sql(offer_id: str, venue_id: str, symbol: str) -> str:
    return f"""INSERT INTO ledger_observation_offer (
      id, observation_id, venue_offer_id, symbol, amount_original,
      amount_remaining, rate_observed, status, mts_created, raw)
      VALUES ('{offer_id}', '{_O}', '{venue_id}', '{symbol}',
      1, 1, true, 'ACTIVE', 1, '{{}}')"""


def _query_sql(
    query_id: str,
    query_revision: int,
    start_revision: int = 0,
    *,
    environment: str = "ci",
    started_at_ms: int = 1,
) -> str:
    return f"""INSERT INTO ledger_observation_query (
      query_id, exchange_account_id, deployment_environment, query_revision,
      started_at_ms, start_revision)
      VALUES ('{query_id}', '{_A}', '{environment}', {query_revision},
              {started_at_ms}, {start_revision})"""


def _basis_sql(
    basis_id: str, observation_id: str, revision: int = 0, *, environment: str = "ci"
) -> str:
    return f"""INSERT INTO accepted_capital_basis (
      id, exchange_account_id, deployment_environment, observation_id,
      accepted, accept_revision, policy_revision_id,
      credit_cells_present, schema_version, digest, accepted_at_ms)
      VALUES ('{basis_id}', '{_A}', '{environment}', '{observation_id}',
      true, {revision}, '{_P}', true, 1, 'basis-digest', 3)"""


@pytest.fixture
def ledger_db(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("ledger_s1_roles", _build))
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed(conn) -> None:
    """Owner inserts one valid row per ledger table so row triggers are exercised."""
    statements = (
        f"INSERT INTO exchange_accounts(id,venue,label) VALUES ('{_A}','bitfinex','ledger')",
        f"""INSERT INTO capital_policy_revisions(id,
      exchange_account_id,
      deployment_environment,
      symbol,
      revision,
      schema_version,
      policy,
      digest,
      source)
      VALUES ('{_P}',
      '{_A}',
      'ci',
      'fUST',
      1,
      1,
      '{{}}',
      'p',
      '{{}}')""",
        f"""INSERT INTO capital_command_clock(exchange_account_id,
      deployment_environment,
      revision)
      VALUES ('{_A}',
      'ci',
      0)""",
        _query_sql(_QID, 1),
        _observation_sql(_O, _QID, accepted=True),
        f"""INSERT INTO ledger_observation_wallet(observation_id,
      wallet_type,
      currency,
      available,
      balance)
      VALUES ('{_O}',
      'funding',
      'UST',
      10,
      10)""",
        _offer_sql(_H, "offer-1", "fUST"),
        f"""INSERT INTO ledger_observation_credit(id,
      observation_id,
      venue_credit_id,
      source_kind,
      symbol,
      amount,
      status,
      raw)
      VALUES ('{_C}',
      '{_O}',
      'credit-1',
      'credit',
      'fUST',
      1,
      'ACTIVE',
      '{{}}')""",
        f"""INSERT INTO ledger_observation_offer_history(id,
      observation_id,
      venue_offer_id,
      symbol,
      amount_original,
      amount_remaining,
      rate_observed,
      status,
      mts_created,
      terminal_kind,
      occurred_at_ms,
      raw)
      VALUES ('{_H}',
      '{_O}',
      'offer-1',
      'fUST',
      1,
      0,
      true,
      'CANCELLED',
      1,
      'cancelled',
      3,
      '{{}}')""",
        f"""INSERT INTO ledger_observation_credit_history(id,
      observation_id,
      venue_credit_id,
      source_kind,
      symbol,
      amount,
      status,
      terminal_kind,
      occurred_at_ms,
      raw)
      VALUES ('{_HC}',
      '{_O}',
      'credit-1',
      'credit',
      'fUST',
      1,
      'CLOSED',
      'closed',
      3,
      '{{}}')""",
        f"""INSERT INTO venue_offer_mirror(exchange_account_id,
      deployment_environment,
      venue_offer_id,
      symbol,
      amount_original,
      amount_remaining,
      rate_observed,
      status,
      mts_created,
      last_accepted_observation_id,
      present_in_latest_accepted_snapshot)
      VALUES ('{_A}',
      'ci',
      'offer-1',
      'fUST',
      1,
      1,
      true,
      'ACTIVE',
      1,
      '{_O}',
      true)""",
        f"""INSERT INTO venue_credit_mirror(exchange_account_id,
      deployment_environment,
      venue_credit_id,
      source_kind,
      symbol,
      amount,
      status,
      last_accepted_observation_id,
      present_in_latest_accepted_snapshot)
      VALUES ('{_A}',
      'ci',
      'credit-1',
      'credit',
      'fUST',
      1,
      'ACTIVE',
      '{_O}',
      true)""",
        _basis_sql(_B, _O),
        f"""INSERT INTO accepted_capital_basis_symbol(basis_id,
      symbol,
      available,
      offered,
      credits,
      unattributed_credits,
      foreign_offers)
      VALUES ('{_B}',
      'fUST',
      10,
      0,
      0,
      0,
      0)""",
        f"""INSERT INTO accepted_capital_basis_cell(basis_id,
      symbol,
      cell_id,
      amount)
      VALUES ('{_B}',
      'fUST',
      'cell',
      10)""",
        f"""INSERT INTO execution_decisions(decision_id,
      account_id,
      exchange_account_id,
      deployment_environment,
      reconcile_id,
      cell_id,
      symbol,
      signal_correlation_id,
      outcome,
      signal_rate,
      amount_usdt,
      duration_days,
      model_evidence,
      safety_result,
      execution_policy,
      service_version,
      config_hash,
      occurred_at_ms,
      recorded_at_ms)
      VALUES ('{_D}',
      'account',
      '{_A}',
      'ci',
      'r',
      'cell',
      'fUST',
      'signal',
      'submitted',
      0,
      1,
      2,
      '{{}}',
      '{{}}',
      'policy',
      'test',
      'hash',
      1,
      1)""",
        f"""INSERT INTO execution_decisions(decision_id,
      account_id,
      exchange_account_id,
      deployment_environment,
      reconcile_id,
      cell_id,
      symbol,
      signal_correlation_id,
      outcome,
      signal_rate,
      amount_usdt,
      duration_days,
      model_evidence,
      safety_result,
      execution_policy,
      service_version,
      config_hash,
      occurred_at_ms,
      recorded_at_ms)
      VALUES ('{_D2}',
      'account',
      '{_A}',
      'ci',
      'r',
      'cell',
      'fUST',
      'signal-2',
      'submitted',
      0,
      1,
      2,
      '{{}}',
      '{{}}',
      'policy',
      'test',
      'hash',
      1,
      1)""",
        f"""INSERT INTO submission_attempt_journal(attempt_id,
      execution_decision_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      attempt_seq,
      normalized_payload,
      payload_sha256,
      basis_id,
      policy_revision_id,
      authorization_evidence,
      started_at_ms)
      VALUES ('{_T}',
      '{_D}',
      '{_A}',
      'ci',
      'fUST',
      1,
      '{{}}',
      'hash',
      '{_B}',
      '{_P}',
      '{{}}',
      4)""",
        f"""INSERT INTO transport_outcome_journal(attempt_id,
      kind,
      venue_offer_id,
      completed_at_ms,
      evidence)
      VALUES ('{_T}',
      'ack',
      'offer-1',
      5,
      '{{}}')""",
        f"""INSERT INTO quarantine_opening(quarantine_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      intended_amount,
      opened_at_ms,
      evidence)
      VALUES ('{_Q}',
      '{_A}',
      'ci',
      'fUST',
      1,
      4,
      '{{}}')""",
        f"""INSERT INTO quarantine_member(quarantine_id,
      venue_offer_id,
      observation_id,
      amount_at_join)
      VALUES ('{_Q}',
      'offer-1',
      '{_O}',
      1)""",
        f"""INSERT INTO execution_resolution_journal(id,
      quarantine_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      action,
      venue_offer_id,
      observation_id,
      actor_kind,
      actor_id,
      resolved_at_ms,
      reason,
      evidence)
      VALUES ('{_R}',
      '{_Q}',
      '{_A}',
      'ci',
      'fUST',
      'bound_to_venue',
      'offer-1',
      '{_O}',
      'operator',
      'test',
      5,
      'test',
      '{{}}')""",
        f"""INSERT INTO accepted_capital_basis_attempt(basis_id,
      attempt_id,
      symbol,
      classification)
      VALUES ('{_B}',
      '{_T}',
      'fUST',
      'reflected')""",
        f"""INSERT INTO accepted_capital_basis_quarantine(basis_id,
      quarantine_id)
      VALUES ('{_B}',
      '{_Q}')""",
    )
    for statement in statements:
        conn.exec_driver_sql(statement)


@pytest.fixture
def seeded(ledger_db):
    with ledger_db.begin() as conn:
        _seed(conn)
    return ledger_db


def test_immutable_rows_reject_update_delete_truncate(seeded) -> None:
    for table in LEDGER_TABLES:
        name = table.name
        if name in {"capital_command_clock", "venue_offer_mirror", "venue_credit_mirror"}:
            continue
        column = next(iter(table.primary_key.columns)).name
        for statement in (
            f"UPDATE {name} SET {column}={column}",
            f"DELETE FROM {name}",
            f"TRUNCATE {name} CASCADE",
        ):
            with (
                seeded.begin() as conn,
                pytest.raises(Exception, match="immutable ledger evidence"),
            ):
                conn.exec_driver_sql(statement)


def test_mirror_terminal_cannot_clear_or_reappear(seeded) -> None:
    for table, evidence, kind in (
        ("venue_offer_mirror", _H, "cancelled"),
        ("venue_credit_mirror", _HC, "closed"),
    ):
        with seeded.begin() as conn:
            conn.exec_driver_sql(
                f"UPDATE {table} SET present_in_latest_accepted_snapshot=false, "
                f"terminal_evidence_id='{evidence}',terminal_kind='{kind}'"
            )
        for statement in (
            f"UPDATE {table} SET terminal_evidence_id=NULL,terminal_kind=NULL",
            f"UPDATE {table} SET present_in_latest_accepted_snapshot=true",
            f"DELETE FROM {table}",
            f"TRUNCATE {table} CASCADE",
        ):
            with seeded.begin() as conn, pytest.raises(Exception, match="immutable ledger"):
                conn.exec_driver_sql(statement)


def test_duplicate_facts_and_scope_mismatch_fail(seeded) -> None:
    with seeded.begin() as conn:
        conn.exec_driver_sql(_offer_sql(str(uuid4()), "offer-other", "fEUR"))
        conn.exec_driver_sql(
            f"""INSERT INTO execution_resolution_journal(id,
      attempt_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      action,
      observation_id,
      actor_kind,
      actor_id,
      resolved_at_ms,
      reason,
      evidence)
      VALUES ('{uuid4()}',
      '{_T}',
      '{_A}',
      'ci',
      'fUST',
      'not_accepted',
      '{_O}',
      'operator',
      'test',
      6,
      'test',
      '{{}}')"""
        )
    cases = (
        (
            f"""INSERT INTO transport_outcome_journal(attempt_id,
      kind,
      completed_at_ms,
      evidence)
      VALUES ('{_T}',
      'unknown',
      6,
      '{{}}')""",
            "duplicate key",
        ),
        (
            f"""INSERT INTO execution_resolution_journal(id,
      quarantine_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      action,
      observation_id,
      actor_kind,
      actor_id,
      resolved_at_ms,
      reason,
      evidence)
      VALUES ('{uuid4()}',
      '{_Q}',
      '{_A}',
      'ci',
      'fUST',
      'not_accepted',
      '{_O}',
      'operator',
      'test',
      6,
      'test',
      '{{}}')""",
            "uq_execution_resolution_quarantine",
        ),
        (
            f"""INSERT INTO execution_resolution_journal(id,
      attempt_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      action,
      observation_id,
      actor_kind,
      actor_id,
      resolved_at_ms,
      reason,
      evidence)
      VALUES ('{uuid4()}',
      '{_T}',
      '{_A}',
      'ci',
      'fUST',
      'not_accepted',
      '{_O}',
      'operator',
      'test',
      7,
      'test',
      '{{}}')""",
            "uq_execution_resolution_attempt",
        ),
        (
            f"""INSERT INTO quarantine_member(quarantine_id,
      venue_offer_id,
      observation_id,
      amount_at_join)
      VALUES ('{_Q}',
      'offer-1',
      '{_O}',
      2)""",
            "duplicate key",
        ),
        (
            f"""INSERT INTO quarantine_member(quarantine_id,
      venue_offer_id,
      observation_id,
      amount_at_join)
      VALUES ('{_Q}',
      'offer-other',
      '{_O}',
      1)""",
            "ledger member scope mismatch",
        ),
        (
            f"""INSERT INTO submission_attempt_journal(attempt_id,
      execution_decision_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      attempt_seq,
      normalized_payload,
      payload_sha256,
      basis_id,
      policy_revision_id,
      authorization_evidence,
      started_at_ms)
      VALUES ('{uuid4()}',
      '{_D2}',
      '{_A}',
      'wrong',
      'fUST',
      2,
      '{{}}',
      'hash',
      '{_B}',
      '{_P}',
      '{{}}',
      4)""",
            "ledger decision scope mismatch",
        ),
        (
            f"""INSERT INTO execution_resolution_journal(id,
      attempt_id,
      exchange_account_id,
      deployment_environment,
      symbol,
      action,
      observation_id,
      actor_kind,
      actor_id,
      resolved_at_ms,
      reason,
      evidence)
      VALUES ('{uuid4()}',
      '{_T}',
      '{_A}',
      'wrong',
      'fUST',
      'not_accepted',
      '{_O}',
      'operator',
      'test',
      6,
      'test',
      '{{}}')""",
            "ledger resolution scope mismatch",
        ),
    )
    for statement, message in cases:
        with seeded.begin() as conn, pytest.raises(Exception, match=message):
            conn.exec_driver_sql(statement)


def test_observation_pair_and_accepted_basis_are_enforced(seeded) -> None:
    other = str(uuid4())
    other_query = str(uuid4())
    with seeded.begin() as conn:
        conn.exec_driver_sql(_query_sql(other_query, 2))
        conn.exec_driver_sql(_observation_sql(other, other_query, accepted=False))
    with seeded.begin() as conn, pytest.raises(Exception, match="fk_accepted_basis_observation"):
        conn.exec_driver_sql(_basis_sql(str(uuid4()), other))
    with (
        seeded.begin() as conn,
        pytest.raises(Exception, match="ledger offer mirror scope mismatch"),
    ):
        conn.exec_driver_sql(
            f"UPDATE venue_offer_mirror SET last_accepted_observation_id='{other}'"
        )
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger basis scope mismatch"):
        conn.exec_driver_sql(_basis_sql(str(uuid4()), _O, environment="wrong"))
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger basis scope mismatch"):
        conn.exec_driver_sql(_basis_sql(str(uuid4()), _O, revision=1))
    query_ids = [str(uuid4()) for _ in range(3)]
    with seeded.begin() as conn:
        for revision, query_id in enumerate(query_ids, start=3):
            conn.exec_driver_sql(_query_sql(query_id, revision))
    with (
        seeded.begin() as conn,
        pytest.raises(Exception, match="ck_ledger_observation_matching_digest"),
    ):
        conn.exec_driver_sql(
            _observation_sql(
                str(uuid4()), query_ids[0], accepted=False, first="x", confirmation="y"
            )
        )
    with seeded.begin() as conn, pytest.raises(Exception, match="ck_ledger_observation_acceptance"):
        conn.exec_driver_sql(
            _observation_sql(str(uuid4()), query_ids[2], accepted=True, complete=False)
        )
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger observation accept fence"):
        conn.exec_driver_sql(
            _observation_sql(str(uuid4()), query_ids[2], accepted=True, accept_revision=1)
        )
    with seeded.begin() as conn, pytest.raises(Exception, match="duplicate key"):
        conn.exec_driver_sql(_observation_sql(str(uuid4()), _QID, accepted=False))


def test_stale_query_and_interleaved_acceptance_fail(seeded) -> None:
    older_query = str(uuid4())
    newer_query = str(uuid4())
    with seeded.begin() as conn:
        conn.exec_driver_sql(_query_sql(older_query, 2))
        conn.exec_driver_sql(_query_sql(newer_query, 3, started_at_ms=2))
    # The first REST call may finish after the second query has already started.
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger observation accept fence"):
        conn.exec_driver_sql(
            _observation_sql(str(uuid4()), older_query, accepted=True, finished_at_ms=5)
        )
    with seeded.begin() as conn:
        conn.exec_driver_sql(_observation_sql(str(uuid4()), newer_query, accepted=True))
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger observation accept fence"):
        conn.exec_driver_sql(
            _observation_sql(str(uuid4()), older_query, accepted=True, finished_at_ms=5)
        )


def test_accept_revision_must_match_query_start_and_current_clock(seeded) -> None:
    query_id = str(uuid4())
    with seeded.begin() as conn:
        conn.exec_driver_sql(_query_sql(query_id, 2))
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger observation accept fence"):
        conn.exec_driver_sql(
            _observation_sql(str(uuid4()), query_id, accepted=True, accept_revision=1)
        )
    with seeded.begin() as conn:
        conn.exec_driver_sql("UPDATE capital_command_clock SET revision=1")
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger observation accept fence"):
        conn.exec_driver_sql(_observation_sql(str(uuid4()), query_id, accepted=True))
    # A command committed after the query started: the clock moved, so matching
    # the current clock alone must not accept an observation of the older query.
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger observation accept fence"):
        conn.exec_driver_sql(
            _observation_sql(str(uuid4()), query_id, accepted=True, accept_revision=1)
        )
    started_at_one = str(uuid4())
    with seeded.begin() as conn:
        conn.exec_driver_sql(_query_sql(started_at_one, 3, start_revision=1))
    with seeded.begin() as conn, pytest.raises(Exception, match="ledger observation accept fence"):
        conn.exec_driver_sql(_observation_sql(str(uuid4()), started_at_one, accepted=True))
    with seeded.begin() as conn:
        conn.exec_driver_sql(
            _observation_sql(str(uuid4()), started_at_one, accepted=True, accept_revision=1)
        )


def test_observation_query_scope_mismatch_fails(seeded) -> None:
    query_id = str(uuid4())
    with seeded.begin() as conn:
        conn.exec_driver_sql(_query_sql(query_id, 1, environment="wrong"))
    with (
        seeded.begin() as conn,
        pytest.raises(Exception, match="ledger observation query scope mismatch"),
    ):
        conn.exec_driver_sql(_observation_sql(str(uuid4()), query_id, accepted=False))


def test_roles_are_read_only_or_exact_writer(seeded) -> None:
    with seeded.connect() as conn:
        for table in LEDGER_TABLES:
            name = table.name
            assert conn.scalar(
                text("SELECT has_any_column_privilege('bfx_cutover_reader',:t,'SELECT')"),
                {"t": name},
            )
            for role in ("bfx_webapi", "bfx_webauth", "bfx_cutover_reader"):
                assert not conn.scalar(
                    text("SELECT has_any_column_privilege(:r,:t,'INSERT')"), {"r": role, "t": name}
                )
                assert not conn.scalar(
                    text("SELECT has_any_column_privilege(:r,:t,'UPDATE')"), {"r": role, "t": name}
                )
                assert not conn.scalar(
                    text("SELECT has_table_privilege(:r,:t,'DELETE')"), {"r": role, "t": name}
                )
            assert not conn.scalar(
                text("SELECT has_table_privilege('bfx_bot',:t,'DELETE')"), {"t": name}
            )
            assert not conn.scalar(
                text("SELECT has_table_privilege('bfx_bot',:t,'TRUNCATE')"), {"t": name}
            )
            if name not in {"capital_command_clock", "venue_offer_mirror", "venue_credit_mirror"}:
                assert not conn.scalar(
                    text("SELECT has_any_column_privilege('bfx_bot',:t,'UPDATE')"), {"t": name}
                )
            assert conn.scalar(
                text("SELECT has_table_privilege('bfx_bot',:t,'INSERT')"), {"t": name}
            )
        for table, column in (
            ("ledger_observation_query", "start_revision"),
            ("ledger_observation", "query_id"),
            ("ledger_observation", "offer_history_pages"),
            ("ledger_observation", "credit_history_pages"),
            ("ledger_observation_offer", "amount_original"),
            ("ledger_observation_offer", "rate_observed"),
            ("accepted_capital_basis", "digest"),
            ("accepted_capital_basis_symbol", "offered"),
            ("accepted_capital_basis_attempt", "classification"),
        ):
            assert conn.scalar(
                text("SELECT has_column_privilege('bfx_cutover_reader',:t,:c,'SELECT')"),
                {"t": table, "c": column},
            )
        for table, column in (
            ("ledger_observation", "evidence"),
            ("ledger_observation_offer", "raw"),
            ("submission_attempt_journal", "normalized_payload"),
            ("submission_attempt_journal", "authorization_evidence"),
        ):
            assert not conn.scalar(
                text("SELECT has_column_privilege('bfx_cutover_reader',:t,:c,'SELECT')"),
                {"t": table, "c": column},
            )
        assert not conn.scalar(
            text("SELECT has_table_privilege('bfx_cutover_reader','ledger_observation','SELECT')")
        )
        assert not conn.scalar(
            text(
                "SELECT has_sequence_privilege('bfx_cutover_reader','trading_state_id_seq','USAGE')"
            )
        )
    for role in ("bfx_bot", "bfx_webapi", "bfx_webauth", "bfx_cutover_reader"):
        for statement in (
            "UPDATE ledger_observation SET accepted=false",
            "DELETE FROM ledger_observation",
        ):
            with seeded.begin() as conn, pytest.raises(Exception, match="permission denied"):
                conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
                conn.exec_driver_sql(statement)
    with seeded.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_cutover_reader")
        assert conn.scalar(text("SELECT count(id) FROM ledger_observation")) == 1
        for table in LEDGER_TABLES:
            column = next(iter(table.primary_key.columns)).name
            conn.exec_driver_sql(f"SELECT {column} FROM {table.name} LIMIT 1")
    with seeded.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql("UPDATE capital_command_clock SET revision=1")
        conn.exec_driver_sql("UPDATE venue_offer_mirror SET amount_remaining=2")
        conn.exec_driver_sql("UPDATE venue_credit_mirror SET amount=2")
    for table in ("capital_command_clock", "venue_offer_mirror", "venue_credit_mirror"):
        for statement in (f"DELETE FROM {table}", f"TRUNCATE {table} CASCADE"):
            with seeded.begin() as conn, pytest.raises(Exception, match="permission denied"):
                conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
                conn.exec_driver_sql(statement)
    with seeded.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_cutover_reader")
        conn.exec_driver_sql("SELECT evidence FROM ledger_observation")
    for role in ("bfx_webapi", "bfx_webauth"):
        with seeded.begin() as conn, pytest.raises(Exception, match="permission denied"):
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            conn.exec_driver_sql("SELECT id FROM ledger_observation")


def test_downgrade_removes_only_its_objects(ledger_db) -> None:
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    alembic(url, "downgrade", "9a4d6e2c7b18")
    with create_engine(url).connect() as conn:
        assert not ({table.name for table in LEDGER_TABLES} & set(inspect(conn).get_table_names()))
        for function in (
            "reject_ledger_mutation",
            "guard_ledger_mirror",
            "guard_ledger_scope",
            "guard_ledger_observation_accept",
        ):
            assert not conn.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname=:n)"), {"n": function}
            )
        # The group was created by the template database, not this clone.
        assert conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bfx_cutover_reader')")
        )


def test_populated_ledger_refuses_downgrade(seeded) -> None:
    url = seeded.url.render_as_string(hide_password=False)
    seeded.dispose()
    with pytest.raises(Exception, match="refuse downgrade of populated ledger"):
        alembic(url, "downgrade", "9a4d6e2c7b18")
