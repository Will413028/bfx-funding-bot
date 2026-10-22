"""The API's CHECK-sourced value sets equal the migrated database's CHECK constraints (9k).

``tests/modules/api/test_literal_sources.py`` binds every response ``Literal`` to its source;
where the source is a column's CHECK constraint, PostgreSQL is the authority, so the values
are read back from the migrated schema (``pg_get_constraintdef``) and compared with the
response type and the domain type it narrows. A migration that adds a value fails here
until the API allows it.
"""
from __future__ import annotations

import re
from typing import get_args

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.accounts.exchange_accounts import (
    AccountLifecycleStatus,
    AccountRole,
    CredentialLifecycleStatus,
)
from bfx_funding_bot.modules.api.schemas import CredentialStatus, ListedAccountStatus
from bfx_funding_bot.modules.api.trading_control import (
    CancelAllPhaseValue,
    TradingStateCause,
    TradingStateValue,
)
from bfx_funding_bot.modules.execution.safety import trading_state

pytestmark = pytest.mark.integration

_CHECK = text("""SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE c.contype = 'c' AND t.relname = :table AND c.conname = :name
    AND t.relnamespace = 'public'::regnamespace""")


@pytest.mark.asyncio
async def test_response_value_sets_equal_the_migrated_check_constraints(migrated_db):
    factory, _ = migrated_db

    async def allowed(table: str, name: str) -> set[str]:
        async with factory() as session:
            definition = (await session.execute(_CHECK, {"table": table, "name": name})).scalar_one()
        return set(re.findall(r"'([^']*)'::text", definition))

    state = await allowed("trading_state", "ck_trading_state_state")
    cause = await allowed("trading_state", "ck_trading_state_cause")
    phase = await allowed("funding_cancel_all_audit", "ck_funding_cancel_all_audit_phase")
    account = await allowed("exchange_accounts", "ck_exchange_accounts_lifecycle_status")
    role = await allowed("exchange_account_memberships", "ck_exchange_account_memberships_role")
    credential = await allowed("exchange_account_credentials",
                               "ck_exchange_account_credentials_lifecycle_status")

    assert set(get_args(TradingStateValue)) == set(trading_state.STATES) == state
    assert set(get_args(TradingStateCause)) == set(trading_state.CAUSES) == cause
    assert set(get_args(CancelAllPhaseValue)) == phase
    assert set(get_args(AccountLifecycleStatus)) == account
    assert set(get_args(ListedAccountStatus)) == account - {"retired"}
    assert set(get_args(AccountRole)) == role
    assert set(get_args(CredentialLifecycleStatus)) == credential
    assert set(get_args(CredentialStatus)) == (
        credential - {"active"} | {"verified", "unverified", "failed"})
