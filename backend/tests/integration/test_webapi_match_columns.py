"""e1f2a3b4c5d7: generated match columns and the web API's column grants for the match preview.

Mutation checks (one at a time; revert after each):

* A generated expression reads another payload key (``'rate'`` -> ``'amount'``):
  ``test_generated_terms_equal_the_payload``.
* Drop the preflight refusal in ``upgrade``: ``test_upgrade_refuses_an_unreadable_rate_or_period``.
* Drop the CHECK / cast: ``test_an_unreadable_term_is_refused_at_write``.
* Grant ``raw`` / ``evidence`` / ``normalized_payload`` to the web API: the allowlist tests in
  ``test_webapi_privilege_allowlist.py`` and ``test_ledger_webapi_grants.py``.
* Downgrade without the REVOKE / DROP: ``test_round_trip_drops_and_restores_the_objects``.
* The web API's context, preview-bind and worker digest cases live in
  ``test_ledger_operator_resolution_pg.py`` (real ``bfx_webapi`` role).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.pg_templates import alembic

from .test_ledger_schema_roles import _O, _seed, ledger_db  # noqa: F401 - fixture
from .test_ledger_webapi_grants import _attempt_with_payload, _columns, _rewrite_payload

pytestmark = pytest.mark.integration

_PREVIOUS = "d0e1f2a3b4c6"
_ATTEMPT_COLUMNS = ("match_rate", "match_period_days", "match_offer_type", "match_flags")
_OBSERVATION_COLUMNS = ("history_symbols", "first_page_counts")


def _terms(conn) -> dict[str, tuple[str | None, ...]]:
    rows = conn.execute(
        text(
            "SELECT execution_decision_id, match_rate::text, match_period_days::text, "
            "match_offer_type, match_flags::text FROM submission_attempt_journal "
            "WHERE execution_decision_id LIKE 'amount-%'"
        )
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


# --- the generated terms -----------------------------------------------------------------------


_PAYLOADS = {
    "full": '{"amount": "5", "rate": "0.0003", "period": 2, "type": "LIMIT", "flags": 0}',
    "numbers": '{"amount": 5, "rate": 0.00045, "period": 30, "type": "LIMIT", "flags": 64}',
    "object": '{"amount": "5", "rate": "1E-4", "period": 2, "type": "LIMIT", "flags": {"hidden": true}}',
    "partial": '{"amount": "5"}',
    "nulls": '{"amount": "5", "rate": null, "period": null, "type": null, "flags": null}',
    "typed": '{"amount": "5", "rate": "1", "period": 2, "type": 7, "flags": true}',
    "other": '{"amount": "5", "size": "9", "daily_rate": "0.5", "duration": 9, "kind": "x"}',
}
_TERMS = {
    "amount-full": ("0.0003", "2", "LIMIT", "0"),
    "amount-numbers": ("0.00045", "30", "LIMIT", "64"),
    "amount-object": ("0.0001", "2", "LIMIT", '{"hidden": true}'),
    "amount-partial": (None, None, None, None),
    "amount-nulls": (None, None, None, None),
    # A type that is not a string and a boolean flags value are not terms; the rest still is.
    "amount-typed": ("1", "2", None, None),
    "amount-other": (None, None, None, None),
}


def test_generated_terms_equal_the_payload(ledger_db) -> None:  # noqa: F811
    with ledger_db.begin() as conn:
        _seed(conn)
        for sequence, (label, payload) in enumerate(_PAYLOADS.items(), start=10):
            _attempt_with_payload(conn, label, payload, sequence=sequence)
    with ledger_db.connect() as conn:
        assert _terms(conn) == _TERMS


def test_the_generated_columns_are_stored_generated_and_not_writable(ledger_db) -> None:  # noqa: F811
    with ledger_db.connect() as conn:
        for table, names in (
            ("submission_attempt_journal", _ATTEMPT_COLUMNS),
            ("ledger_observation", _OBSERVATION_COLUMNS),
        ):
            for name in names:
                row = conn.execute(
                    text(
                        "SELECT is_nullable, is_generated, generation_expression FROM "
                        "information_schema.columns WHERE table_name=:t AND column_name=:c"
                    ),
                    {"t": table, "c": name},
                ).one()
                assert (row.is_nullable, row.is_generated) == ("YES", "ALWAYS"), (table, name)
                source = "normalized_payload" if table.startswith("submission") else "evidence"
                assert source in row.generation_expression, (table, name)
    with ledger_db.begin() as conn:
        _seed(conn)
    with ledger_db.begin() as conn, pytest.raises(Exception, match="generated column"):
        conn.exec_driver_sql("UPDATE submission_attempt_journal SET match_rate = 1")
    with ledger_db.begin() as conn, pytest.raises(Exception, match="generated column"):
        conn.exec_driver_sql("UPDATE ledger_observation SET history_symbols = '[]'::jsonb")


_UNREADABLE = [
    '{"amount": "5", "rate": "abc", "period": 2}',
    '{"amount": "5", "rate": "", "period": 2}',
    '{"amount": "5", "rate": "NaN", "period": 2}',
    '{"amount": "5", "rate": "Infinity", "period": 2}',
    '{"amount": "5", "rate": true, "period": 2}',
    '{"amount": "5", "rate": "0.1", "period": "x"}',
    '{"amount": "5", "rate": "0.1", "period": 2.5}',
    '{"amount": "5", "rate": "0.1", "period": 99999999999}',
]


@pytest.mark.parametrize("payload", _UNREADABLE)
def test_upgrade_refuses_an_unreadable_rate_or_period(ledger_db, payload) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            _seed(conn)
            _rewrite_payload(conn, payload)
        with pytest.raises(RuntimeError, match="refuse upgrade"):
            alembic(url, "upgrade", "head")
        with engine.connect() as conn:
            assert not set(_ATTEMPT_COLUMNS) & set(_columns(conn, "submission_attempt_journal"))
            assert not set(_OBSERVATION_COLUMNS) & set(_columns(conn, "ledger_observation"))
            assert conn.scalar(
                text("SELECT has_column_privilege('bfx_webapi', 'ledger_observation_offer', 'rate', 'SELECT')")
            ) is False
    finally:
        engine.dispose()


@pytest.mark.parametrize("payload", _UNREADABLE)
def test_an_unreadable_term_is_refused_at_write(ledger_db, payload) -> None:  # noqa: F811
    with ledger_db.begin() as conn:
        _seed(conn)
    with ledger_db.begin() as conn, pytest.raises(Exception, match=r"match_rate|match_period|invalid input|out of range"):
        _attempt_with_payload(conn, "unreadable", payload, sequence=10)


# --- upgrade from the previous head on a database with rows -------------------------------------------


def test_upgrade_from_the_previous_head_fills_existing_rows(ledger_db) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            _seed(conn)
            for sequence, (label, payload) in enumerate(_PAYLOADS.items(), start=10):
                _attempt_with_payload(conn, label, payload, sequence=sequence)
            conn.exec_driver_sql("ALTER TABLE ledger_observation DISABLE TRIGGER USER")
            conn.exec_driver_sql(
                "UPDATE ledger_observation SET evidence = "
                """'{"history_symbols": ["fUST", "fUSD"], "first_page_counts": {"wallet": 1, "offer": 2}}'::jsonb """
                f"WHERE id = '{_O}'"
            )
            conn.exec_driver_sql("ALTER TABLE ledger_observation ENABLE TRIGGER USER")
        alembic(url, "upgrade", "head")
        with engine.connect() as conn:
            assert _terms(conn) == _TERMS
            symbols, counts = conn.execute(
                text(f"SELECT history_symbols::text, first_page_counts::text FROM ledger_observation WHERE id='{_O}'")
            ).one()
            assert symbols == '["fUST", "fUSD"]' and counts == '{"offer": 2, "wallet": 1}'
            assert conn.scalar(
                text("SELECT has_column_privilege('bfx_webapi', 'ledger_observation', 'history_symbols', 'SELECT')")
            ) is True
        alembic(url, "check")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("evidence", "symbols", "counts"),
    [
        ('{}', None, None),
        ('{"history_symbols": null, "first_page_counts": null}', None, None),
        ('{"history_symbols": "fUST", "first_page_counts": [1]}', None, None),
        ('{"history_symbols": [], "first_page_counts": {}}', "[]", "{}"),
    ],
)
def test_evidence_columns_follow_the_json_type(ledger_db, evidence, symbols, counts) -> None:  # noqa: F811
    with ledger_db.begin() as conn:
        _seed(conn)
        conn.exec_driver_sql("ALTER TABLE ledger_observation DISABLE TRIGGER USER")
        conn.exec_driver_sql(f"UPDATE ledger_observation SET evidence = '{evidence}'::jsonb")
        conn.exec_driver_sql("ALTER TABLE ledger_observation ENABLE TRIGGER USER")
    with ledger_db.connect() as conn:
        assert tuple(
            conn.execute(
                text("SELECT history_symbols::text, first_page_counts::text FROM ledger_observation")
            ).one()
        ) == (symbols, counts)


# --- round trip ------------------------------------------------------------------------------------------


def _objects(conn) -> dict[str, bool]:
    return {
        "attempt_columns": set(_ATTEMPT_COLUMNS) <= set(_columns(conn, "submission_attempt_journal")),
        "observation_columns": set(_OBSERVATION_COLUMNS) <= set(_columns(conn, "ledger_observation")),
        "check": bool(
            conn.scalar(text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_submission_attempt_match_rate'"))
        ),
        "offer_grant": bool(
            conn.scalar(text("SELECT has_column_privilege('bfx_webapi','ledger_observation_offer','rate','SELECT')"))
        ),
    }


def test_round_trip_drops_and_restores_the_objects(ledger_db) -> None:  # noqa: F811
    url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            assert _objects(conn) == dict.fromkeys(
                ("attempt_columns", "observation_columns", "check", "offer_grant"), True)
        alembic(url, "downgrade", _PREVIOUS)
        with engine.connect() as conn:
            assert _objects(conn) == dict.fromkeys(
                ("attempt_columns", "observation_columns", "check", "offer_grant"), False)
        with engine.begin() as conn:
            _seed(conn)
            _rewrite_payload(conn, _PAYLOADS["full"])
        alembic(url, "upgrade", "head")
        with engine.connect() as conn:
            assert all(_objects(conn).values())
            assert conn.scalar(text("SELECT match_rate::text FROM submission_attempt_journal")) == "0.0003"
        alembic(url, "check")
    finally:
        engine.dispose()
