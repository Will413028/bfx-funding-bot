"""Operator authority is membership alone (quality plan 9d-1, ``e489261c10b1``).

At head ``public.operator_authorized`` and the daemon's ``operator_authorized`` read only
``exchange_account_memberships`` and ``exchange_accounts``: they keep working with the
``auth`` schema gone (9d-7 drops it). The migration asserts the new verdict equals the old
one and names at most one user, and the owner script that adds the tailnet node user ID
(W3) refuses anything that would authorize a third user.

Mutations (one at a time; revert after each):

* join ``auth."user"`` again in the new body: the migration's own "no function reads
  auth" assertion refuses it, so every test here errors while building its database (and
  ``test_authority_survives_dropping_auth`` would fail without that assertion);
* drop ``FOR SHARE OF m,a``: ``test_the_verdict_holds_its_membership_until_commit`` fails;
* drop the migration's equality assertion: ``test_the_migration_refuses_a_member_the_old_
  verdict_refused`` fails;
* drop the migration's single-user assertion: ``test_the_migration_refuses_two_authorized_
  users`` fails;
* remove the script's digits check: ``test_the_script_refuses_a_non_numeric_node_id`` fails;
* remove the script's cap: ``test_the_script_refuses_a_third_authorized_user`` fails.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from bfx_funding_bot.core.db import make_session_factory
from bfx_funding_bot.modules.execution.operator_requests import operator_authorized
from scripts.grant_operator_membership import GrantRefusedError, grant
from tests.pg_templates import alembic, stamp_realm

from .test_trading_state_migration import _reset

pytestmark = pytest.mark.integration

_BEFORE = "36e59526f8cf"
_ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_OPERATOR = "operator-old-id"
_NODE = "1234567890"


def _build(target: str):
    def build(url: str) -> None:
        _reset(create_engine(url))
        alembic(url, "upgrade", target)
        stamp_realm(url, "ci")
    return build


def _user(conn, user_id: str, *, admin: bool = True, totp: bool = True) -> None:
    conn.execute(text(
        'INSERT INTO auth."user" (id,name,email,"emailVerified","createdAt","updatedAt",'
        'role,banned,"twoFactorEnabled") VALUES (:u, :u, :e, true, now(), now(), :r, false, :t)'),
        {"u": user_id, "e": f"{user_id}@test.invalid", "r": "admin" if admin else "user",
         "t": totp})


def _member(conn, user_id: str, role: str = "owner", account: UUID = _ACCOUNT) -> None:
    conn.execute(text("INSERT INTO exchange_account_memberships(exchange_account_id,user_id,role) "
                      "VALUES (:a,:u,:r)"), {"a": account, "u": user_id, "r": role})


def _account(conn, account: UUID = _ACCOUNT, status: str = "active") -> None:
    conn.execute(text("INSERT INTO exchange_accounts(id, venue, label, lifecycle_status) "
                      "VALUES (:a, 'bitfinex', 'x', :s)"), {"a": account, "s": status})


def _async_engine(engine):
    return create_async_engine(
        engine.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg"))


def _authorized(engine, user: str, account: UUID = _ACCOUNT) -> bool:
    with engine.begin() as conn:
        return bool(conn.scalar(text("SELECT public.operator_authorized(:a, :u)"),
                                {"a": account, "u": user}))


@pytest.fixture
def head(pg_templates, pg_clone):
    engine = create_engine(pg_clone(pg_templates.template("membership_authority_head",
                                                          _build("head"))))
    with engine.begin() as conn:
        _account(conn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def before(pg_templates, pg_clone):
    url = pg_clone(pg_templates.template("membership_authority_before", _build(_BEFORE)))
    engine = create_engine(url)
    with engine.begin() as conn:
        _account(conn)
    try:
        yield url, engine
    finally:
        engine.dispose()


def test_authority_survives_dropping_auth(head) -> None:
    with head.begin() as conn:
        _member(conn, _OPERATOR)
        conn.exec_driver_sql("DROP SCHEMA auth CASCADE")
    assert _authorized(head, _OPERATOR)
    assert not _authorized(head, "stranger")


@pytest.mark.parametrize(("role", "status", "expected"), [
    ("owner", "active", True), ("operator", "halted", True),
    ("viewer", "active", False), ("owner", "retired", False)])
def test_only_an_owner_or_operator_of_a_live_account(head, role, status, expected) -> None:
    with head.begin() as conn:
        conn.execute(text("UPDATE exchange_accounts SET lifecycle_status = :s WHERE id = :a"),
                     {"s": status, "a": _ACCOUNT})
        _member(conn, _OPERATOR, role)
    assert _authorized(head, _OPERATOR) is expected


def test_the_verdict_holds_its_membership_until_commit(head) -> None:
    with head.begin() as conn:
        _member(conn, _OPERATOR)
    with head.connect() as holder:
        holder.begin()
        assert holder.scalar(text("SELECT public.operator_authorized(:a, :u)"),
                             {"a": _ACCOUNT, "u": _OPERATOR})
        with head.begin() as other, pytest.raises(Exception, match="lock timeout"):
            other.exec_driver_sql("SET LOCAL lock_timeout = '200ms'")
            other.execute(text("DELETE FROM exchange_account_memberships WHERE user_id = :u"),
                          {"u": _OPERATOR})
        holder.rollback()


def test_the_release_archive_check_refuses_everyone(head) -> None:
    with head.begin() as conn:
        _member(conn, _OPERATOR)
        assert conn.scalar(text("SELECT release_archive.release_operator_authorized(:a, :u)"),
                           {"a": _ACCOUNT, "u": _OPERATOR}) is False


async def test_the_daemon_authority_needs_no_configured_operator(head, monkeypatch) -> None:
    """The configured operator id no longer gates the daemon: membership decides."""
    monkeypatch.setenv("BFX_OPERATOR_USER_ID", "someone-else")
    with head.begin() as conn:
        _member(conn, _OPERATOR)
    engine = _async_engine(head)
    try:
        async with make_session_factory(engine)() as session:
            assert await operator_authorized(session, account_id=_ACCOUNT, user=_OPERATOR)
            assert not await operator_authorized(session, account_id=_ACCOUNT, user="stranger")
            assert not await operator_authorized(session, account_id=_ACCOUNT, user="")
    finally:
        await engine.dispose()


def test_the_migration_keeps_todays_operator(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        _user(conn, _OPERATOR)
        _member(conn, _OPERATOR)
        _member(conn, "not-in-auth", "viewer")
    alembic(url, "upgrade", "head")
    assert _authorized(engine, _OPERATOR)


def test_the_migration_refuses_a_member_the_old_verdict_refused(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        _user(conn, _OPERATOR, totp=False)
        _member(conn, _OPERATOR)
    with pytest.raises(Exception, match="differs from the old verdict"):
        alembic(url, "upgrade", "head")


def test_the_migration_refuses_two_authorized_users(before) -> None:
    url, engine = before
    with engine.begin() as conn:
        for user in (_OPERATOR, "second-admin"):
            _user(conn, user)
            _member(conn, user)
    with pytest.raises(Exception, match="more than one user"):
        alembic(url, "upgrade", "head")


async def _grant(engine, *, from_user: str = _OPERATOR, node: str = _NODE, role: str | None = None):
    async_engine = _async_engine(engine)
    try:
        async with make_session_factory(async_engine)() as session:
            if role:
                await session.execute(text(f"SET LOCAL ROLE {role}"))
            try:
                report = await grant(session, from_user=from_user, node_user_id=node)
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return report
    finally:
        await async_engine.dispose()


async def test_the_script_copies_memberships_and_is_idempotent(head) -> None:
    with head.begin() as conn:
        _member(conn, _OPERATOR, "operator")
    first = await _grant(head)
    assert (first["inserted"], first["authorized_users"]) == (1, 2)
    assert (await _grant(head))["inserted"] == 0
    with head.begin() as conn:
        assert conn.scalar(text("SELECT role FROM exchange_account_memberships WHERE user_id=:u"),
                           {"u": _NODE}) == "operator"
    assert _authorized(head, _NODE)


async def test_the_script_refuses_a_non_numeric_node_id(head) -> None:
    with head.begin() as conn:
        _member(conn, _OPERATOR)
    with pytest.raises(GrantRefusedError, match="node_user_id_not_numeric"):
        await _grant(head, node="1234; DROP")


async def test_the_script_refuses_a_role_that_does_not_own_the_table(head) -> None:
    with head.begin() as conn:
        _member(conn, _OPERATOR)
    with pytest.raises(GrantRefusedError, match="not_the_table_owner"):
        await _grant(head, role="bfx_webapi")


async def test_the_script_refuses_an_unknown_source(head) -> None:
    with pytest.raises(GrantRefusedError, match="from_user_has_no_membership"):
        await _grant(head)


async def test_the_script_refuses_a_third_authorized_user(head) -> None:
    with head.begin() as conn:
        _member(conn, _OPERATOR)
        _member(conn, "second-member")
    with pytest.raises(GrantRefusedError, match="too_many_authorized_users"):
        await _grant(head)
    with head.begin() as conn:
        assert conn.scalar(text("SELECT count(*) FROM exchange_account_memberships "
                                "WHERE user_id=:u"), {"u": _NODE}) == 0
