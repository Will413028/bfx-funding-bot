"""``execution.capital_policy_read`` on clone PostgreSQL: the pre-trade guards' unlocked policy
read refuses a scope without a head (``policy_unavailable``) and a head that does not point at
its own revision (``inconsistent_policy_pointer``), and the web API's trading status shows the
refusal as that currency's ``policy_error`` without hiding the others.

A broken pointer cannot be written through the policy writer; it is written here behind the
triggers, as a corrupt row would look.

Mutation checks (one at a time; revert after each):

* Skip ``check_pointer`` in ``read_policy_row``: ``test_a_head_off_its_own_revision_refuses``
  fails (the foreign revision is parsed and returned).
* Return ``None`` instead of refusing a missing head: ``test_no_head_is_policy_unavailable``
  fails.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.api.trading_control import _currencies
from bfx_funding_bot.modules.execution.capital_policy_read import (
    CapitalBlockedError,
    read_policy_unlocked,
)
from bfx_funding_bot.modules.trading import (
    CapitalPolicy,
    policy_digest,
    policy_payload,
    policy_schema_version,
)

from .test_ledger_basis import _engine
from .test_ledger_schema_roles import _A, ledger_db  # noqa: F401 - fixture re-export

pytestmark = pytest.mark.integration
ACCOUNT = UUID(_A)


@pytest.fixture
def db(ledger_db):  # noqa: F811
    with ledger_db.begin() as conn:
        conn.exec_driver_sql(
            f"INSERT INTO exchange_accounts(id,venue,label) VALUES ('{_A}','bitfinex','policy')")
    return ledger_db


def _revision(engine: Engine, symbol: str, revision: int, *, enabled: bool = True) -> UUID:
    payload = policy_payload(CapitalPolicy(enabled=enabled))
    revision_id = uuid4()
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO capital_policy_revisions(id, exchange_account_id, deployment_environment, "
            "symbol, revision, schema_version, policy, digest, source) "
            "VALUES (:id, :a, 'ci', :s, :r, :v, CAST(:p AS jsonb), :d, '{}')"), {
                "id": revision_id, "a": _A, "s": symbol, "r": revision,
                "v": policy_schema_version(CapitalPolicy(enabled=enabled)),
                "p": json.dumps(payload), "d": policy_digest(payload)})
    return revision_id


def _head(engine: Engine, symbol: str, revision_id: UUID, revision: int) -> None:
    """A head row, behind the triggers (a corrupt pointer is not the writer's to make)."""
    with engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL session_replication_role = replica")
        conn.execute(text(
            "INSERT INTO capital_policy_heads(exchange_account_id, deployment_environment, "
            "symbol, revision_id, revision) VALUES (:a, 'ci', :s, :id, :r)"),
            {"a": _A, "s": symbol, "id": revision_id, "r": revision})


def _run(engine: Engine, read: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    async def run() -> Any:
        async_engine = _engine(engine)
        try:
            async with AsyncSession(async_engine) as session, session.begin():
                return await read(session)
        finally:
            await async_engine.dispose()

    return asyncio.run(run())


def _read(engine: Engine, symbol: str) -> CapitalPolicy | str:
    """The applied policy, or the refusal's reason."""
    async def read(session: AsyncSession) -> CapitalPolicy | str:
        try:
            return await read_policy_unlocked(session, account_id=ACCOUNT, environment="ci",
                                              symbol=symbol)
        except CapitalBlockedError as exc:
            return str(exc)

    return _run(engine, read)


def test_no_head_is_policy_unavailable(db) -> None:
    _revision(db, "fUST", 1)  # a revision alone is not applied
    assert _read(db, "fUST") == "policy_unavailable"


def test_a_head_at_its_own_revision_reads_the_policy(db) -> None:
    _head(db, "fUST", _revision(db, "fUST", 1), 1)
    assert _read(db, "fUST") == CapitalPolicy(enabled=True)


@pytest.mark.parametrize("broken", ["revision_number", "other_symbol"])
def test_a_head_off_its_own_revision_refuses(db, broken: str) -> None:
    if broken == "revision_number":
        _head(db, "fUST", _revision(db, "fUST", 1), 2)
    else:
        _head(db, "fUST", _revision(db, "fUSD", 1), 1)
    assert _read(db, "fUST") == "inconsistent_policy_pointer"


def test_trading_status_shows_the_refusal_as_that_currencys_policy_error(db) -> None:
    _head(db, "fUSD", _revision(db, "fUSD", 1, enabled=False), 1)
    _head(db, "fUST", _revision(db, "fUST", 1), 2)
    currencies = _run(db, lambda session: _currencies(session, (ACCOUNT, "ci")))
    assert [(c["symbol"], c["policy_error"], c["enabled"]) for c in currencies] == [
        ("fUSD", None, False), ("fUST", "inconsistent_policy_pointer", None)]
