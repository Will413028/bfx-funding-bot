"""c9d0e1f2a3b5: the web API's column-level ledger read grants, the generated attempt amount.

Mutation checks (one at a time; revert after each):

* ``GRANT SELECT`` table-wide on ``ledger_observation``: the denied-column case fails.
* Add ``quarantine_opening.evidence`` or ``submission_attempt_journal.normalized_payload``
  to the grant: ``test_every_column_is_granted_exactly_per_the_allowlist``.
* Downgrade without REVOKE: ``test_round_trip_drops_and_restores_the_objects``.
* Generated expression reads another payload key: ``test_generated_amount_equals_the_payload_amount``.
* Drop the preflight refusal: ``test_upgrade_refuses_an_unusable_payload_amount``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.pg_templates import alembic

from .test_ledger_schema_roles import _D, _seed, ledger_db  # noqa: F401 - fixture

pytestmark = pytest.mark.integration

_PREVIOUS = "b8c9d0e1f2a4"
_FLAGS = (
    "wallets_complete", "offers_complete", "credits_complete", "loans_complete",
    "offer_history_complete", "credit_history_complete", "trades_complete",
)
# Written out here, not imported from the migration: the test is the second opinion.
# At head: e1f2a3b4c5d7 added the match columns (``test_webapi_match_columns.py``); the
# ones it added are marked below.
_OBSERVED_OFFER = {
    "observation_id", "venue_offer_id", "symbol", "amount_original", "amount_remaining", "rate",
    "rate_observed", "period_days", "offer_type", "flags", "status", "mts_created", "mts_updated",
}
ALLOWED: dict[str, set[str]] = {
    "ledger_observation_query": {
        "query_id", "exchange_account_id", "deployment_environment", "query_revision",
        "started_at_ms",
    },
    "ledger_observation": {
        "id", "query_id", "exchange_account_id", "deployment_environment", "accepted",
        "query_finished_at_ms", "first_digest", "confirmation_digest", *_FLAGS,
        # e1f2a3b4c5d7
        "history_requested_start_ms", "history_requested_end_ms", "history_oldest_mts_created",
        "history_newest_mts_created", "offer_history_pages", "credit_history_pages",
        "trades_requested_start_ms", "trades_requested_end_ms", "history_symbols",
        "first_page_counts",
    },
    "ledger_observation_offer": _OBSERVED_OFFER,
    "ledger_observation_offer_history": {*_OBSERVED_OFFER, "terminal_kind", "occurred_at_ms"},
    "accepted_capital_basis": {
        "id", "exchange_account_id", "deployment_environment", "observation_id",
        "accept_revision", "attempt_seq_high_water", "accepted_at_ms",
    },
    "accepted_capital_basis_attempt": {"basis_id", "attempt_id", "symbol", "classification"},
    "accepted_capital_basis_quarantine": {"basis_id", "quarantine_id"},
    "accepted_capital_basis_symbol": {
        "basis_id", "symbol", "available", "offered", "credits", "unattributed_credits",
    },
    "accepted_capital_basis_credit": {"basis_id", "symbol"},
    "submission_attempt_journal": {
        "attempt_id", "execution_decision_id", "exchange_account_id", "deployment_environment",
        "symbol", "cell_id", "attempt_seq", "started_at_ms", "intended_amount",
        # e1f2a3b4c5d7
        "match_rate", "match_period_days", "match_offer_type", "match_flags",
    },
    "transport_outcome_journal": {
        "attempt_id", "kind", "venue_offer_id", "reason", "completed_at_ms",
    },
    "quarantine_opening": {
        "quarantine_id", "exchange_account_id", "deployment_environment", "symbol",
        "intended_amount", "opened_at_ms", "opened_revision", "source_attempt_id",
    },
    "execution_resolution_journal": {
        "id", "attempt_id", "quarantine_id", "exchange_account_id", "deployment_environment",
        "symbol", "action", "venue_offer_id", "actor_kind", "actor_id", "operator_request_id",
        "resolved_at_ms", "reason",
    },
    "venue_offer_mirror": {
        "exchange_account_id", "deployment_environment", "venue_offer_id", "symbol",
        "amount_original", "amount_remaining", "mts_updated",
        "present_in_latest_accepted_snapshot",
    },
}
# Ledger tables the web API must not read at all.
UNGRANTED = (
    "ledger_observation_wallet", "ledger_observation_credit",
    "ledger_observation_credit_history",
    "ledger_observation_trade", "venue_credit_mirror", "quarantine_member",
    "accepted_capital_basis_cell", "accepted_capital_basis_credit_cell",
)
# Evidence-bearing columns that stay denied whatever else is granted.
DENIED = (
    ("ledger_observation", "evidence"),
    ("ledger_observation_offer", "raw"),
    ("ledger_observation_offer", "id"),
    ("ledger_observation_offer_history", "raw"),
    ("ledger_observation_offer_history", "id"),
    ("accepted_capital_basis", "scope_block"),
    ("accepted_capital_basis", "digest"),
    ("accepted_capital_basis_symbol", "block"),
    ("accepted_capital_basis_symbol", "conservation"),
    ("accepted_capital_basis_symbol", "lent_unexplained"),
    ("accepted_capital_basis_symbol", "foreign_executed"),
    ("submission_attempt_journal", "normalized_payload"),
    ("submission_attempt_journal", "authorization_evidence"),
    ("submission_attempt_journal", "seed_provenance"),
    ("submission_attempt_journal", "payload_sha256"),
    ("submission_attempt_journal", "basis_id"),
    ("submission_attempt_journal", "policy_revision_id"),
    ("transport_outcome_journal", "evidence"),
    ("quarantine_opening", "evidence"),
    ("execution_resolution_journal", "evidence"),
    ("execution_resolution_journal", "observation_id"),
)


def _columns(conn, table: str) -> list[str]:
    return list(
        conn.scalars(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
            ),
            {"t": table},
        )
    )


def _webapi_select(conn, table: str, column: str) -> None:
    conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
    conn.exec_driver_sql(f'SELECT "{column}" FROM {table} LIMIT 1')


def test_every_column_is_granted_exactly_per_the_allowlist(ledger_db) -> None:  # noqa: F811
    with ledger_db.connect() as conn:
        for table, allowed in ALLOWED.items():
            columns = _columns(conn, table)
            assert allowed <= set(columns), (table, allowed - set(columns))
            for column in columns:
                granted = conn.scalar(
                    text("SELECT has_column_privilege('bfx_webapi', :t, :c, 'SELECT')"),
                    {"t": table, "c": column},
                )
                assert granted is (column in allowed), (table, column)
        for table in UNGRANTED:
            for column in _columns(conn, table):
                assert not conn.scalar(
                    text("SELECT has_column_privilege('bfx_webapi', :t, :c, 'SELECT')"),
                    {"t": table, "c": column},
                ), (table, column)
        for table in ALLOWED:
            assert not conn.scalar(
                text("SELECT has_table_privilege('bfx_webapi', :t, 'SELECT')"), {"t": table}
            ), table
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                assert not conn.scalar(
                    text("SELECT has_table_privilege('bfx_webapi', :t, :p)"),
                    {"t": table, "p": privilege},
                ), (table, privilege)


def test_allowlisted_columns_read_and_denied_columns_refuse_a_real_select(ledger_db) -> None:  # noqa: F811
    for table, allowed in ALLOWED.items():
        for column in sorted(allowed):
            with ledger_db.begin() as conn:
                _webapi_select(conn, table, column)
    for table, column in DENIED:
        with ledger_db.begin() as conn, pytest.raises(Exception, match="permission denied"):
            _webapi_select(conn, table, column)
    for table in UNGRANTED:
        with ledger_db.begin() as conn, pytest.raises(Exception, match="permission denied"):
            _webapi_select(conn, table, _columns(conn, table)[0])
    # A whole-row read needs every column: the allowlist alone never satisfies it.
    with ledger_db.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.exec_driver_sql("SELECT * FROM ledger_observation")


def _attempt_with_payload(conn, label: str, payload: str, *, sequence: int) -> None:
    """One more attempt of the seeded scope whose ``normalized_payload`` is ``payload``."""
    decision = f"amount-{label}"
    conn.exec_driver_sql(
        f"""INSERT INTO execution_decisions(decision_id, account_id, exchange_account_id,
      deployment_environment, reconcile_id, cell_id, symbol, signal_correlation_id, outcome,
      signal_rate, amount_usdt, duration_days, model_evidence, safety_result,
      execution_policy, service_version, config_hash, occurred_at_ms, recorded_at_ms)
      SELECT '{decision}', account_id, exchange_account_id, deployment_environment,
      reconcile_id, cell_id, symbol, 'signal-{label}', outcome, signal_rate, amount_usdt,
      duration_days, model_evidence, safety_result, execution_policy, service_version,
      config_hash, occurred_at_ms, recorded_at_ms
      FROM execution_decisions WHERE decision_id = '{_D}'"""
    )
    conn.exec_driver_sql(
        f"""INSERT INTO submission_attempt_journal(attempt_id, execution_decision_id,
      exchange_account_id, deployment_environment, symbol, cell_id, attempt_seq,
      normalized_payload, payload_sha256, basis_id, policy_revision_id,
      authorization_evidence, started_at_ms)
      SELECT gen_random_uuid(), '{decision}', exchange_account_id, deployment_environment,
      symbol, cell_id, {sequence}, '{payload}'::jsonb, 'hash', basis_id,
      policy_revision_id, '{{}}', 4
      FROM submission_attempt_journal WHERE execution_decision_id = '{_D}'"""
    )


def _amounts(conn) -> dict[str, str | None]:
    rows = conn.execute(
        text(
            "SELECT execution_decision_id, intended_amount::text "
            "FROM submission_attempt_journal WHERE execution_decision_id LIKE 'amount-%'"
        )
    ).all()
    return dict(rows)


def test_generated_amount_equals_the_payload_amount(ledger_db) -> None:  # noqa: F811
    payloads = {
        "string": '{"amount": "12.50"}',
        "number": '{"amount": 7}',
        "zero": '{"amount": "0"}',
        "other": '{"amount": "5", "size": "99"}',
    }
    with ledger_db.begin() as conn:
        _seed(conn)
        for sequence, (label, payload) in enumerate(payloads.items(), start=10):
            _attempt_with_payload(conn, label, payload, sequence=sequence)
    with ledger_db.connect() as conn:
        assert _amounts(conn) == {
            "amount-string": "12.50", "amount-number": "7", "amount-zero": "0",
            "amount-other": "5",
        }


@pytest.mark.parametrize(
    "payload", ['{"amount": "-1"}', '{"amount": "NaN"}', '{"amount": "Infinity"}']
)
def test_a_negative_or_nonfinite_amount_is_refused_by_the_check(ledger_db, payload) -> None:  # noqa: F811
    with ledger_db.begin() as conn:
        _seed(conn)
    with ledger_db.begin() as conn, pytest.raises(
        Exception, match="ck_submission_attempt_intended_amount"
    ):
        _attempt_with_payload(conn, "bad", payload, sequence=10)


@pytest.mark.parametrize("payload", ['{}', '{"amount": null}', '{"symbol": "fUST"}'])
def test_an_attempt_without_an_amount_is_refused_at_write(ledger_db, payload) -> None:  # noqa: F811
    with ledger_db.begin() as conn:
        _seed(conn)
    with ledger_db.begin() as conn, pytest.raises(Exception, match="intended_amount"):
        _attempt_with_payload(conn, "missing", payload, sequence=10)


def test_the_generated_amount_column_is_not_null(ledger_db) -> None:  # noqa: F811
    with ledger_db.connect() as conn:
        assert conn.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns WHERE "
                "table_name='submission_attempt_journal' AND column_name='intended_amount'"
            )
        ) == "NO"


def test_the_generated_amount_is_not_writable(ledger_db) -> None:  # noqa: F811
    with ledger_db.begin() as conn:
        _seed(conn)
    with ledger_db.begin() as conn, pytest.raises(Exception, match="generated column"):
        conn.exec_driver_sql(
            "INSERT INTO submission_attempt_journal(attempt_id, intended_amount) "
            "VALUES (gen_random_uuid(), 1)"
        )


def _rewrite_payload(conn, payload: str) -> None:
    """Rewrite the seeded attempt's payload past its immutability trigger (old-revision setup)."""
    conn.exec_driver_sql("ALTER TABLE submission_attempt_journal DISABLE TRIGGER USER")
    conn.exec_driver_sql(
        f"UPDATE submission_attempt_journal SET normalized_payload = '{payload}'::jsonb"
    )
    conn.exec_driver_sql("ALTER TABLE submission_attempt_journal ENABLE TRIGGER USER")


@pytest.mark.parametrize(
    "payload",
    ['{}', '{"amount": null}', '{"amount": "abc"}', '{"amount": "-1"}', '{"amount": "NaN"}',
     '{"amount": "Infinity"}', '{"amount": ""}'],
)
def test_upgrade_refuses_an_unusable_payload_amount(ledger_db, payload) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            _seed(conn, pre_verdict=True)
            _rewrite_payload(conn, payload)
        with pytest.raises(RuntimeError, match="refuse upgrade"):
            alembic(url, "upgrade", "head")
        with engine.connect() as conn:
            assert "intended_amount" not in _columns(conn, "submission_attempt_journal")
    finally:
        engine.dispose()


def _objects(conn) -> dict[str, bool]:
    return {
        "column": "intended_amount" in _columns(conn, "submission_attempt_journal"),
        "check": bool(
            conn.scalar(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'ck_submission_attempt_intended_amount'"
                )
            )
        ),
        "index": bool(
            conn.scalar(
                text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_execution_resolution_scope_resolved'")
            )
        ),
        "grant": bool(
            conn.scalar(text("SELECT has_column_privilege('bfx_webapi','ledger_observation','id','SELECT')"))
        ),
    }


def test_round_trip_drops_and_restores_the_objects(ledger_db) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert _objects(conn) == dict.fromkeys(("column", "check", "index", "grant"), True)
        alembic(url, "downgrade", _PREVIOUS)
        with engine.connect() as conn:
            assert _objects(conn) == dict.fromkeys(("column", "check", "index", "grant"), False)
            # Every granted column is revoked, not just the first table's.
            for table, columns in ALLOWED.items():
                for column in columns & set(_columns(conn, table)):
                    assert not conn.scalar(
                        text("SELECT has_column_privilege('bfx_webapi', :t, :c, 'SELECT')"),
                        {"t": table, "c": column},
                    ), (table, column)
        with engine.begin() as conn:
            _seed(conn, pre_verdict=True)
            _rewrite_payload(conn, '{"amount": "12.5"}')
        alembic(url, "upgrade", "head")
        with engine.connect() as conn:
            assert _objects(conn) == dict.fromkeys(("column", "check", "index", "grant"), True)
            assert conn.scalar(
                text("SELECT intended_amount::text FROM submission_attempt_journal")
            ) == "12.5"
        alembic(url, "check")
    finally:
        engine.dispose()


def test_alembic_check_is_clean_at_head(ledger_db) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    alembic(url, "check")
