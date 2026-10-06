"""G8: an uncertainty resolution request may cite a ledger observation (schema only).

Mutation checks (one at a time; revert after each):

* Drop ``ck_uncertainty_resolution_requests_evidence``. Both-NULL and both-set rows insert.
* Let the ledger branch of ``ck_..._outcome_shape`` accept a ``resolved_event_seq``.
* Drop ``observation_id`` from the guard's immutable list. The UPDATE succeeds.
* Drop the applied-requires-journal rule from the guard. The journal-less apply succeeds.
* Drop ``uq_execution_resolution_operator_request``. Two journal rows share a request.
* Drop the ``uncertainty_request_evidence_epoch`` trigger. The webapi epoch cases insert.
* Make the downgrade skip its refusal. The populated downgrade succeeds.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bfx_funding_bot.modules.execution.operator_requests import insert_request
from bfx_funding_bot.modules.execution.uncertainty_tables import UncertaintyResolutionRequestRow
from tests.pg_templates import alembic

from .test_ledger_schema_roles import _A, _O, _build, _seed, append_epoch, pre_switch

pytestmark = pytest.mark.integration

_PREVIOUS = "a7b8c9d0e1f3"
_TABLE = "uncertainty_resolution_requests"


@pytest.fixture
def seeded(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("ledger_s1_roles", _build))
    engine = create_engine(url)
    with engine.begin() as conn:
        pre_switch(conn)  # the request evidence rule is tested across the switch
        _seed(conn)
    try:
        yield engine
    finally:
        engine.dispose()


def _request(
    *, seq: int | None = 7, observation: str | None = None, request_id=None, uncertainty=None,
    extra: str = "", extra_values: str = "",
) -> str:
    seq_sql = "NULL" if seq is None else str(seq)
    obs_sql = "NULL" if observation is None else f"'{observation}'"
    return f"""INSERT INTO {_TABLE}(request_id, exchange_account_id, deployment_environment,
      uncertainty_id, action, reconcile_event_seq, observation_id, requested_by, created_at_ms{extra})
      VALUES ('{request_id or uuid4()}', '{_A}', 'ci', '{uncertainty or uuid4()}',
      'mark_not_accepted', {seq_sql}, {obs_sql}, 'op', 1000{extra_values})"""


def _ledger_request(conn, request_id=None) -> str:
    request_id = str(request_id or uuid4())
    conn.exec_driver_sql(_request(seq=None, observation=_O, request_id=request_id))
    return request_id


def _quarantine(conn) -> str:
    quarantine = str(uuid4())
    conn.exec_driver_sql(
        f"""INSERT INTO quarantine_opening(quarantine_id, exchange_account_id,
      deployment_environment, symbol, intended_amount, opened_at_ms, opened_revision, evidence)
      VALUES ('{quarantine}', '{_A}', 'ci', 'fUST', 1, 4, 1, '{{}}')"""
    )
    return quarantine


def _journal(conn, request_id: str | None, *, quarantine: str | None = None) -> None:
    quarantine = quarantine or _quarantine(conn)
    request_sql = "NULL" if request_id is None else f"'{request_id}'"
    conn.exec_driver_sql(
        f"""INSERT INTO execution_resolution_journal(id, quarantine_id, exchange_account_id,
      deployment_environment, symbol, action, observation_id, actor_kind, actor_id,
      operator_request_id, resolved_at_ms, reason, evidence)
      VALUES ('{uuid4()}', '{quarantine}', '{_A}', 'ci', 'fUST', 'not_accepted', '{_O}',
      'operator', 'test', {request_sql}, 6, 'test', '{{}}')"""
    )


def _apply_as_bot(conn, request_id: str, *, resolved_event_seq: str = "NULL") -> None:
    conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
    conn.exec_driver_sql(
        f"UPDATE {_TABLE} SET state='applied', processed_at_ms=2000, "
        f"resolved_event_seq={resolved_event_seq} WHERE request_id='{request_id}'"
    )


def _set_epoch(conn, authority: str) -> None:
    append_epoch(conn, authority, "switch")


def test_exactly_one_evidence_column(seeded) -> None:
    with seeded.begin() as conn:
        conn.exec_driver_sql(_request(seq=7))
        conn.exec_driver_sql(_request(seq=None, observation=_O))
    for sql in (_request(seq=None), _request(seq=7, observation=_O)):
        with seeded.begin() as conn, pytest.raises(Exception, match="_evidence"):
            conn.exec_driver_sql(sql)


def test_insert_request_swallows_an_evidence_violation_as_slot_taken(seeded) -> None:
    url = seeded.url.render_as_string(hide_password=False)
    values = {
        "request_id": uuid4(), "exchange_account_id": _A, "deployment_environment": "ci",
        "uncertainty_id": uuid4(), "action": "mark_not_accepted", "reconcile_event_seq": 7,
        "observation_id": _O, "venue_offer_id": None, "decision": None, "reason": None,
        "requested_by": "op", "created_at_ms": 1,
    }

    async def run(values: dict[str, object]) -> bool:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn, AsyncSession(bind=conn) as session:
                return await insert_request(session, UncertaintyResolutionRequestRow, values)
        finally:
            await engine.dispose()

    values["exchange_account_id"] = UUID(str(_A))
    values["observation_id"] = UUID(str(_O))
    # Known behaviour: every IntegrityError reads as "pending slot taken".
    assert asyncio.run(run(values)) is False
    # Same values with one evidence column is accepted, so the False above is the XOR CHECK.
    assert asyncio.run(run({**values, "observation_id": None})) is True


def test_outcome_shape_follows_the_evidence_kind(seeded) -> None:
    with seeded.begin() as conn:
        ledger = _ledger_request(conn)
        legacy = str(uuid4())
        conn.exec_driver_sql(_request(seq=7, request_id=legacy))
    # Ledger row: applied must not carry a resolved_event_seq.
    with seeded.begin() as conn:
        _journal(conn, ledger)
    with seeded.begin() as conn, pytest.raises(Exception, match="outcome_shape"):
        _apply_as_bot(conn, ledger, resolved_event_seq="1")
    with seeded.begin() as conn:
        _apply_as_bot(conn, ledger)
    # Legacy row: applied still requires one.
    with seeded.begin() as conn, pytest.raises(Exception, match="outcome_shape"):
        _apply_as_bot(conn, legacy)
    with seeded.begin() as conn:
        assert conn.scalar(text(f"SELECT state FROM {_TABLE} WHERE request_id=:r"), {"r": ledger}) == (
            "applied"
        )


def test_guard_pins_observation_and_requires_a_journal_row(seeded) -> None:
    with seeded.begin() as conn:
        ledger = _ledger_request(conn)
    with seeded.begin() as conn, pytest.raises(Exception, match="immutable uncertainty resolution request"):
        conn.exec_driver_sql(f"UPDATE {_TABLE} SET observation_id=NULL, reconcile_event_seq=7")
    with seeded.begin() as conn, pytest.raises(Exception, match="immutable uncertainty resolution request"):
        conn.exec_driver_sql(f"UPDATE {_TABLE} SET observation_id='{uuid4()}'")
    with seeded.begin() as conn, pytest.raises(Exception, match="without a journal row"):
        _apply_as_bot(conn, ledger)
    # A journal row citing a different request does not count.
    with seeded.begin() as conn:
        other = _ledger_request(conn)
        _journal(conn, other)
    with seeded.begin() as conn, pytest.raises(Exception, match="without a journal row"):
        _apply_as_bot(conn, ledger)
    with seeded.begin() as conn:
        _journal(conn, ledger)
    with seeded.begin() as conn:
        _apply_as_bot(conn, ledger)
    # A rejection never needs a journal row.
    with seeded.begin() as conn:
        third = _ledger_request(conn)
        conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
        conn.exec_driver_sql(
            f"UPDATE {_TABLE} SET state='rejected', processed_at_ms=2, outcome_reason='x' "
            f"WHERE request_id='{third}'"
        )


def test_journal_operator_request_is_unique_but_nullable(seeded) -> None:
    with seeded.begin() as conn:
        first, second = _ledger_request(conn), _ledger_request(conn)
        _journal(conn, first)
        _journal(conn, None)
        _journal(conn, None)  # NULLs never collide
        _journal(conn, second)
    with seeded.begin() as conn, pytest.raises(Exception, match="uq_execution_resolution_operator_request"):
        _journal(conn, first)


def test_webapi_insert_follows_the_authority_epoch(seeded) -> None:
    def as_webapi(sql: str) -> None:
        with seeded.begin() as conn:
            conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
            conn.exec_driver_sql(sql)

    with seeded.connect() as conn:
        assert conn.scalar(
            text(f"SELECT has_column_privilege('bfx_webapi','{_TABLE}','observation_id','INSERT')")
        )
        assert not conn.scalar(
            text(f"SELECT has_column_privilege('bfx_bot','{_TABLE}','observation_id','INSERT')")
        )
        assert not conn.scalar(
            text(f"SELECT has_column_privilege('bfx_webapi','{_TABLE}','observation_id','UPDATE')")
        )
    # Legacy epoch.
    with pytest.raises(Exception, match="requires ledger authority"):
        as_webapi(_request(seq=None, observation=_O))
    as_webapi(_request(seq=7))
    # The owner is exempt under either epoch.
    with seeded.begin() as conn:
        conn.exec_driver_sql(_request(seq=None, observation=_O))
        _set_epoch(conn, "ledger")
        conn.exec_driver_sql(_request(seq=7))
    # Ledger epoch.
    with pytest.raises(Exception, match="closed under ledger authority"):
        as_webapi(_request(seq=7))
    as_webapi(_request(seq=None, observation=_O))


def test_migration_round_trip_and_populated_downgrade(seeded) -> None:
    url = seeded.url.render_as_string(hide_password=False)
    with seeded.begin() as conn:
        _ledger_request(conn)
    seeded.dispose()
    with pytest.raises(Exception, match="refuse downgrade with ledger-evidence rows"):
        alembic(url, "downgrade", _PREVIOUS)
    with seeded.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE {_TABLE} DISABLE TRIGGER uncertainty_resolution_request_no_delete")
        conn.exec_driver_sql(f"DELETE FROM {_TABLE}")
        conn.exec_driver_sql(f"ALTER TABLE {_TABLE} ENABLE TRIGGER uncertainty_resolution_request_no_delete")
    seeded.dispose()
    alembic(url, "downgrade", _PREVIOUS)
    with seeded.connect() as conn:
        columns = {c["name"]: c for c in inspect(conn).get_columns(_TABLE)}
        assert "observation_id" not in columns
        assert not columns["reconcile_event_seq"]["nullable"]
        assert "guard_uncertainty_request_evidence_epoch" not in set(
            conn.scalars(text("SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace"))
        )
        assert "operator_request" not in " ".join(
            i["name"] for i in inspect(conn).get_indexes("execution_resolution_journal")
        )
        conn.rollback()
    seeded.dispose()
    alembic(url, "upgrade", "head")
    alembic(url, "check")
    with seeded.connect() as conn:
        assert "observation_id" in {c["name"] for c in inspect(conn).get_columns(_TABLE)}
