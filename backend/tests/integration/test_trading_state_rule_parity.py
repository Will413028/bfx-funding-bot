"""Python's trading-state pre-check refuses exactly what PostgreSQL refuses.

The insert trigger and CHECKs are the authority; ``check_transition`` exists
only to fail earlier with a clearer error. Every (previous, next) pair below is
decided by both, and the verdicts must agree. The one deliberate difference:
code reads "no decision recorded" as HALTED, while the trigger accepts any
first row -- there Python may only be stricter.
"""
from __future__ import annotations

import itertools
from uuid import uuid4

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.execution.safety.trading_state import (
    IllegalTradingTransition,
    check_transition,
    read_current,
)

pytestmark = pytest.mark.integration

# REDUCING, material_deploy and kill_switch are retired: both sides must refuse them.
STATES = ("ACTIVE", "HALTED", "REDUCING")
CAUSES = ("operator", "auto", "material_deploy", "kill_switch")
# Histories that set up each "previous" situation; the last row is the current state.
HISTORIES = {
    "none": [],
    "ACTIVE/operator": [("ACTIVE", "operator")],
    "ACTIVE/operator-after-halt": [("ACTIVE", "operator"), ("HALTED", "auto"), ("ACTIVE", "operator")],
    "HALTED/operator": [("ACTIVE", "operator"), ("HALTED", "operator")],
    "HALTED/auto": [("ACTIVE", "operator"), ("HALTED", "auto")],
}
NEXT = list(itertools.product(STATES, CAUSES))

_INSERT = text("""INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause,
    actor, reason, created_at_ms) VALUES (:a, :env, :state, :cause, 'test', 'test', 1)""")


@pytest.mark.asyncio
async def test_python_and_postgresql_refuse_the_same_transitions(migrated_db):
    factory, account = migrated_db
    disagreements = []
    for (name, history), (state, cause) in itertools.product(HISTORIES.items(), NEXT):
        env = f"parity-{uuid4().hex[:10]}"
        async with factory.begin() as session:
            for row_state, row_cause in history:
                await session.execute(_INSERT, {"a": account, "env": env, "state": row_state,
                                                "cause": row_cause})
        async with factory() as session:
            current = await read_current(session, account_id=account, environment=env)
        try:
            await check_transition(current=current, state=state, cause=cause, actor="test",
                                   reason="test")
            python_refuses = False
        except IllegalTradingTransition:
            python_refuses = True
        try:
            async with factory.begin() as session:
                await session.execute(_INSERT, {"a": account, "env": env, "state": state,
                                                "cause": cause})
            database_refuses = False
        except Exception:
            database_refuses = True
        if python_refuses != database_refuses and not (name == "none" and python_refuses):
            disagreements.append((name, state, cause, python_refuses, database_refuses))
    assert disagreements == []
