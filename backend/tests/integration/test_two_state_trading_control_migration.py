"""5b1e7c9d2a40: REDUCING, probation and build approvals retire without losing history.

On a disposable PostgreSQL: rows recorded under the previous rules (a
probation, a material-deploy pause, an approval, approve/pause requests) are
archived or kept as recorded, a current pause becomes an operator HALTED, and a
downgrade puts everything back byte for byte.
"""
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

from .test_trading_state_migration import _alembic, _reset

pytestmark = pytest.mark.integration

_BEFORE = "1f6392809120"
_A = "00000000-0000-0000-0000-0000000000a1"
_B = "00000000-0000-0000-0000-0000000000b1"
_DIG = "sha256:" + "a" * 64
_REQ = "00000000-0000-0000-0000-0000000000d1"


def _state(conn, account: str, state: str, cause: str, *, probation: bool = False) -> int:
    extra = (", probation_multiplier, probation_started_at_ms, probation_floor",
             ", 0.25, 5, '{\"fUST\": \"150.75\"}'") if probation else ("", "")
    return conn.scalar(text(f"""INSERT INTO trading_state (exchange_account_id, deployment_environment,
        state, cause, actor, reason, created_at_ms{extra[0]})
        VALUES (:a, 'prod', :s, :c, 'will', :r, 5{extra[1]}) RETURNING id"""),
        {"a": account, "s": state, "c": cause, "r": f"{state}/{cause}"})


def _snapshot(conn, schema_requests: str, schema_approvals: str) -> dict[str, list[tuple]]:
    return {
        "states": [tuple(r) for r in conn.execute(text(
            "SELECT id, state, cause, reason FROM public.trading_state ORDER BY id"))],
        "probation": [tuple(r) for r in conn.execute(text(
            f"SELECT {'trading_state_id' if schema_requests == 'release_archive' else 'id'},"
            " probation_multiplier, probation_started_at_ms, probation_floor::text FROM "
            + ("release_archive.trading_state_probation" if schema_requests == "release_archive"
               else "public.trading_state WHERE probation_multiplier IS NOT NULL")
            + " ORDER BY 1"))],
        "requests": [tuple(r) for r in conn.execute(text(
            f"SELECT request_id, action, backend_digest, state FROM {schema_requests}."
            + ("trading_control_requests_v1" if schema_requests == "release_archive"
               else "trading_control_requests") + " ORDER BY request_id"))],
        "approvals": [tuple(r) for r in conn.execute(text(
            f"SELECT backend_digest, approved_by FROM {schema_approvals}.deployment_approvals"))],
    }


@pytest.fixture
def before(pg_container):
    url = pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    engine = create_engine(url)
    _reset(engine)
    _alembic(url, "upgrade", _BEFORE)
    ids: dict[str, int] = {}
    with engine.begin() as conn:
        for account in (_A, _B):
            conn.execute(text("INSERT INTO exchange_accounts(id, venue, label) VALUES (:a, 'bitfinex', 'x')"),
                         {"a": account})
        # _A: a probation, then a material deploy parked it -- the current row is REDUCING.
        _state(conn, _A, "ACTIVE", "operator")
        ids["probation"] = _state(conn, _A, "ACTIVE", "operator", probation=True)
        ids["pause"] = _state(conn, _A, "REDUCING", "material_deploy")
        # _B: trading plainly.
        _state(conn, _B, "ACTIVE", "operator")
        conn.execute(text("""INSERT INTO deployment_approvals (exchange_account_id, deployment_environment,
            backend_digest, source_revision, approved_by, approved_at_ms, request_id)
            VALUES (:a, 'prod', :d, :r, 'will', 6, :q)"""), {"a": _A, "d": _DIG, "r": "c" * 40, "q": _REQ})
        conn.execute(text("""INSERT INTO trading_control_requests (request_id, exchange_account_id,
            deployment_environment, action, backend_digest, reason, requested_by, created_at_ms, state,
            processed_at_ms, outcome_reason) VALUES (:q, :a, 'prod', 'approve', :d, 'reviewed', 'will', 6,
            'applied', 7, 'approved')"""), {"q": _REQ, "a": _A, "d": _DIG})
        recorded = _snapshot(conn, "public", "public")
    try:
        yield url, engine, ids, recorded
    finally:
        engine.dispose()


def test_upgrade_archives_the_retired_rules_and_stops_the_current_pause(before):
    url, engine, ids, recorded = before
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")
    with engine.begin() as conn:
        after = _snapshot(conn, "release_archive", "release_archive")
        # Every recorded row is still there, as recorded; one row is appended.
        assert after["states"][:-1] == recorded["states"]
        assert after["states"][-1][1:3] == ("HALTED", "operator")
        assert f"carried over from trading_state {ids['pause']}" in after["states"][-1][3]
        assert after["probation"] == recorded["probation"]
        assert after["requests"] == recorded["requests"]
        assert after["approvals"] == recorded["approvals"]
        for column in ("probation_multiplier", "probation_started_at_ms", "probation_floor"):
            assert conn.scalar(text("SELECT count(*) FROM information_schema.columns WHERE "
                                    "table_name='trading_state' AND column_name=:c"), {"c": column}) == 0
        assert conn.scalar(text("SELECT to_regclass('public.deployment_approvals')")) is None
        manifest = {row.table_name for row in conn.execute(text(
            "SELECT table_name FROM release_archive.manifest WHERE archived_by_revision='5b1e7c9d2a40'"))}
        assert manifest == {"trading_state_probation", "deployment_approvals", "trading_control_requests_v1"}
        # The new request table is empty and takes resume/kill only, naming no build.
        assert conn.scalar(text("SELECT count(*) FROM public.trading_control_requests")) == 0
    # A legacy REDUCING still counts as stopped, and the archive is frozen.
    with engine.begin() as conn, pytest.raises(Exception, match="HALTED -> ACTIVE by auto"):
        conn.execute(text("""INSERT INTO trading_state (exchange_account_id, deployment_environment, state,
            cause, actor, reason, created_at_ms) VALUES (:a, 'prod', 'ACTIVE', 'auto', 'x', 'x', 9)"""),
            {"a": _A})
    with engine.begin() as conn, pytest.raises(Exception, match="release_archive is frozen"):
        conn.exec_driver_sql("DELETE FROM release_archive.trading_state_probation")
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE bfx_webapi")
        conn.execute(text("""INSERT INTO trading_control_requests (request_id, exchange_account_id,
            deployment_environment, action, reason, requested_by, created_at_ms)
            VALUES ('00000000-0000-0000-0000-0000000000e1', :a, 'prod', 'resume', 'x', 'will', 9)"""),
            {"a": _A})


def test_downgrade_restores_every_recorded_row(before):
    url, engine, _ids, recorded = before
    _alembic(url, "upgrade", "head")
    with engine.begin() as conn:
        appended = conn.execute(text(
            "SELECT id, state, cause, reason FROM trading_state ORDER BY id DESC LIMIT 1")).one()
    _alembic(url, "downgrade", _BEFORE)
    with engine.begin() as conn:
        restored = _snapshot(conn, "public", "public")
        assert restored["states"] == [*recorded["states"], tuple(appended)]
        assert {k: v for k, v in restored.items() if k != "states"} == {
            k: v for k, v in recorded.items() if k != "states"}
        assert conn.scalar(text("SELECT to_regclass('release_archive.trading_state_probation')")) is None
        assert conn.scalar(text(
            "SELECT count(*) FROM release_archive.manifest WHERE archived_by_revision='5b1e7c9d2a40'")) == 0
        # The old grants hold again.
        assert conn.scalar(text(
            "SELECT has_table_privilege('bfx_bot', 'deployment_approvals', 'INSERT')"))
        assert conn.scalar(text(
            "SELECT has_column_privilege('bfx_webapi', 'trading_control_requests', 'backend_digest', 'INSERT')"))
    _alembic(url, "upgrade", "head")
    _alembic(url, "check")


def test_downgrade_refuses_while_new_requests_exist(before):
    url, engine, _ids, _recorded = before
    _alembic(url, "upgrade", "head")
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO trading_control_requests (request_id, exchange_account_id,
            deployment_environment, action, reason, requested_by, created_at_ms)
            VALUES ('00000000-0000-0000-0000-0000000000e2', :a, 'prod', 'kill', 'x', 'will', 9)"""),
            {"a": UUID(_B)})
    import os
    import subprocess

    from .test_trading_state_migration import _BACKEND
    result = subprocess.run(["uv", "run", "alembic", "downgrade", _BEFORE], cwd=_BACKEND,
                            env=dict(os.environ, DATABASE_URL=url), capture_output=True, text=True)
    assert result.returncode != 0
    assert "refuse downgrade of recorded resume/kill requests" in result.stdout + result.stderr
