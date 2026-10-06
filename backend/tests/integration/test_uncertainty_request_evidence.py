"""An uncertainty resolution request cites a ledger observation, and only that (schema only).

``b8c9d0e1f2a4`` let a request cite a ledger observation instead of a reconcile event;
``e4f5a6b7c8d9`` closed the reconcile side: ``observation_id`` is NOT NULL, no role may write
``reconcile_event_seq`` or ``resolved_event_seq``, and the epoch trigger is gone. Its round
trip is compared, catalog object by catalog object, with a database built only to the
revision before it.

Mutation checks (one at a time; revert after each):

* Drop ``ck_uncertainty_resolution_requests_evidence``. The both-set row inserts.
* Skip ``e4f5a6b7c8d9``'s ``SET NOT NULL``. The reconcile-only row inserts (and ``alembic
  check`` reports the model's NOT NULL).
* Let the ledger branch of ``ck_..._outcome_shape`` accept a ``resolved_event_seq``.
* Drop ``observation_id`` from the guard's immutable list. The UPDATE succeeds.
* Drop the applied-requires-journal rule from the guard. The journal-less apply succeeds.
* Drop ``uq_execution_resolution_operator_request``. Two journal rows share a request.
* Skip ``e4f5a6b7c8d9``'s precondition. ``test_contract_refuses_a_request_with_pre_switch_
  evidence`` fails (the upgrade then fails on the NOT NULL instead, with another message).
* Skip one grant, or the trigger, in ``e4f5a6b7c8d9``'s downgrade: the round trip differs.
* Make b8c9d0e1f2a4's downgrade skip its refusal. The populated downgrade succeeds.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from bfx_funding_bot.modules.execution.operator_requests import insert_request
from bfx_funding_bot.modules.execution.uncertainty_tables import UncertaintyResolutionRequestRow
from tests.pg_templates import alembic, stamp_realm

from .test_ledger_schema_roles import (
    _A,
    _O,
    _build,
    _seed,
    ledger_db,  # noqa: F401 - fixture re-export
    pre_switch,
)
from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_PREVIOUS = "a7b8c9d0e1f3"
_BEFORE_CONTRACT = "d3e4f5a6b7c8"
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
    # The closed column is named only when it is set: naming it at all needs a grant.
    seq_column = "" if seq is None else " reconcile_event_seq,"
    seq_sql = "" if seq is None else f" {seq},"
    obs_sql = "NULL" if observation is None else f"'{observation}'"
    return f"""INSERT INTO {_TABLE}(request_id, exchange_account_id, deployment_environment,
      uncertainty_id, action,{seq_column} observation_id, requested_by, created_at_ms{extra})
      VALUES ('{request_id or uuid4()}', '{_A}', 'ci', '{uncertainty or uuid4()}',
      'mark_not_accepted',{seq_sql} {obs_sql}, 'op', 1000{extra_values})"""


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


def _apply_as_bot(conn, request_id: str, *, resolved_event_seq: str | None = None) -> None:
    resolved = "" if resolved_event_seq is None else f", resolved_event_seq={resolved_event_seq}"
    conn.exec_driver_sql("SET LOCAL ROLE bfx_bot")
    conn.exec_driver_sql(
        f"UPDATE {_TABLE} SET state='applied', processed_at_ms=2000{resolved} "
        f"WHERE request_id='{request_id}'"
    )


def test_a_request_cites_an_observation_and_never_a_reconcile_event(seeded) -> None:
    with seeded.begin() as conn:
        conn.exec_driver_sql(_request(seq=None, observation=_O))
    # Even the owner, whom no grant limits.
    for sql, refusal in (
        (_request(seq=None), "observation_id"),
        (_request(seq=7), "observation_id"),
        (_request(seq=7, observation=_O), "_evidence"),
    ):
        with seeded.begin() as conn, pytest.raises(Exception, match=refusal):
            conn.exec_driver_sql(sql)


def test_insert_request_swallows_an_evidence_violation_as_slot_taken(seeded) -> None:
    url = seeded.url.render_as_string(hide_password=False)
    values = {
        "request_id": uuid4(), "exchange_account_id": _A, "deployment_environment": "ci",
        "uncertainty_id": uuid4(), "action": "mark_not_accepted",
        "observation_id": None, "venue_offer_id": None, "decision": None, "reason": None,
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
    # No observation (the request columns no longer name the reconcile event).
    # Known behaviour: every IntegrityError reads as "pending slot taken".
    assert asyncio.run(run(values)) is False
    # Same values with the observation are accepted, so the False above is its NOT NULL.
    assert asyncio.run(run({**values, "observation_id": UUID(str(_O))})) is True


def test_an_applied_request_carries_no_resolved_event(seeded) -> None:
    with seeded.begin() as conn:
        ledger = _ledger_request(conn)
        _journal(conn, ledger)
    # The owner (no grant stops it) still meets the outcome-shape CHECK...
    with seeded.begin() as conn, pytest.raises(Exception, match="outcome_shape"):
        conn.exec_driver_sql(
            f"UPDATE {_TABLE} SET state='applied', processed_at_ms=2000, resolved_event_seq=1 "
            f"WHERE request_id='{ledger}'")
    # ...and the account writer has no grant on the column at all.
    with seeded.begin() as conn, pytest.raises(Exception, match="permission denied"):
        _apply_as_bot(conn, ledger, resolved_event_seq="1")
    with seeded.begin() as conn:
        _apply_as_bot(conn, ledger)
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


def test_only_the_observation_is_the_web_apis_to_write(seeded) -> None:
    def as_role(role: str, sql: str) -> None:
        with seeded.begin() as conn:
            conn.exec_driver_sql(f"SET LOCAL ROLE {role}")
            conn.exec_driver_sql(sql)

    with seeded.connect() as conn:
        assert conn.scalar(
            text(f"SELECT has_column_privilege('bfx_webapi','{_TABLE}','observation_id','INSERT')")
        )
        for role, column in (
            ("bfx_bot", "observation_id"), ("bfx_webapi", "reconcile_event_seq"),
            ("bfx_bot", "reconcile_event_seq"), ("bfx_webapi", "resolved_event_seq"),
            ("bfx_bot", "resolved_event_seq"),
        ):
            assert not conn.scalar(text(
                f"SELECT has_column_privilege('{role}','{_TABLE}','{column}','INSERT, UPDATE')"
            )), (role, column)
        assert not conn.scalar(
            text(f"SELECT has_column_privilege('bfx_webapi','{_TABLE}','observation_id','UPDATE')")
        )
    # No epoch gates the observation any more: the epoch row is irrelevant (this clone's latest
    # is ``legacy``, which the evidence epoch trigger would have refused).
    as_role("bfx_webapi", _request(seq=None, observation=_O))
    with pytest.raises(Exception, match="permission denied"):
        as_role("bfx_webapi", _request(seq=7, observation=_O))


# -- e4f5a6b7c8d9: the contract step against a database built only to the revision before -------

_CATALOG = {
    "column": f"""SELECT att.attname, format_type(att.atttypid, att.atttypmod), att.attnotnull,
        COALESCE((SELECT array_agg(CASE a.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(a.grantee) END || ':' || a.privilege_type
                    || ':' || a.is_grantable ORDER BY 1)
                  FROM aclexplode(att.attacl) a), '{{}}')::text
        FROM pg_attribute att WHERE att.attrelid = 'public.{_TABLE}'::regclass
        AND att.attnum > 0 AND NOT att.attisdropped""",
    "table_acl": f"""SELECT CASE a.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
        a.privilege_type, a.is_grantable
        FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = 'public.{_TABLE}'::regclass""",
    "constraint": f"""SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'public.{_TABLE}'::regclass""",
    "index": f"""SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index
        WHERE indrelid = 'public.{_TABLE}'::regclass""",
    "trigger": f"""SELECT tgname, pg_get_triggerdef(oid), tgenabled FROM pg_trigger
        WHERE tgrelid = 'public.{_TABLE}'::regclass AND NOT tgisinternal""",
    "function": """SELECT p.oid::regprocedure::text, pg_get_functiondef(p.oid),
        COALESCE((SELECT array_agg(CASE a.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(a.grantee) END || ':' || a.privilege_type ORDER BY 1)
                  FROM aclexplode(p.proacl) a), '{}')::text
        FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
        AND strpos(p.proname, 'uncertainty') > 0""",
}


def _catalog(url: str) -> dict[str, set[tuple[object, ...]]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {kind: {tuple(row) for row in conn.exec_driver_sql(sql)}
                    for kind, sql in _CATALOG.items()}
    finally:
        engine.dispose()


def _build_before_contract(url: str) -> None:
    """``test_ledger_schema_roles._build``, stopped at the revision before the contract."""
    engine = create_engine(url)
    _reset(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bfx_webauth') "
            "THEN CREATE ROLE bfx_webauth; END IF; END $$")
        conn.exec_driver_sql("GRANT USAGE ON SCHEMA public TO bfx_webauth")
        conn.exec_driver_sql(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO bfx_webauth")
    engine.dispose()
    alembic(url, "upgrade", _BEFORE_CONTRACT)
    stamp_realm(url, "ci")


@pytest.fixture
def before_contract(pg_templates, pg_clone) -> str:
    return pg_clone(pg_templates.template(f"request_evidence_{_BEFORE_CONTRACT}",
                                          _build_before_contract))


def test_contract_round_trip_restores_the_previous_catalog(before_contract, ledger_db) -> None:  # noqa: F811
    before = _catalog(before_contract)
    head_url = ledger_db.url.render_as_string(hide_password=False)
    ledger_db.dispose()
    head = _catalog(head_url)
    # What the upgrade changes, and nothing else.
    epoch = "guard_uncertainty_request_evidence_epoch()"
    assert head["function"] < before["function"]
    assert {row[0] for row in before["function"] - head["function"]} == {epoch}
    assert head["trigger"] < before["trigger"]
    assert {row[0] for row in before["trigger"] - head["trigger"]} == {
        "uncertainty_request_evidence_epoch"}
    # PostgreSQL 18 records a NOT NULL as a constraint too.
    assert head["constraint"] - before["constraint"] == {
        ("uncertainty_resolution_requests_observation_id_not_null", "NOT NULL observation_id")}
    assert before["constraint"] <= head["constraint"]
    assert (head["table_acl"], head["index"]) == (before["table_acl"], before["index"])
    columns_before = {row[0]: row[1:] for row in before["column"]}
    columns_head = {row[0]: row[1:] for row in head["column"]}
    changed = {name for name in columns_before if columns_before[name] != columns_head[name]}
    assert changed == {"observation_id", "reconcile_event_seq", "resolved_event_seq"}
    assert columns_head["observation_id"][1] is True  # NOT NULL
    assert columns_head["reconcile_event_seq"][2] == columns_head["resolved_event_seq"][2] == "{}"
    assert columns_before["reconcile_event_seq"][2] == "{bfx_webapi:INSERT:false}"
    assert columns_before["resolved_event_seq"][2] == "{bfx_bot:UPDATE:false}"
    # The downgrade restores the previous revision exactly; upgrading again restores head.
    alembic(head_url, "downgrade", _BEFORE_CONTRACT)
    assert _catalog(head_url) == before
    alembic(head_url, "upgrade", "head")
    alembic(head_url, "check")
    assert _catalog(head_url) == head


def test_contract_refuses_a_request_with_pre_switch_evidence(before_contract) -> None:
    """A pre-switch applied request (both columns set; the CHECKs allow no other shape)."""
    engine = create_engine(before_contract)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"INSERT INTO exchange_accounts(id,venue,label) VALUES ('{_A}','bitfinex','x')")
            # The owner: the evidence epoch trigger exempts it.
            conn.exec_driver_sql(_request(seq=7))
            conn.exec_driver_sql(
                f"UPDATE {_TABLE} SET state='applied', processed_at_ms=2, resolved_event_seq=8")
        with pytest.raises(Exception, match="refuse to close the pre-switch evidence columns: 1"):
            alembic(before_contract, "upgrade", "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _BEFORE_CONTRACT
            assert conn.scalar(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'uncertainty_request_evidence_epoch'"
            )) == 1
    finally:
        engine.dispose()


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
