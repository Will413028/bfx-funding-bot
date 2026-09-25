"""Python's trading-state pre-check refuses exactly what PostgreSQL refuses.

The insert triggers and CHECKs are the authority; ``check_transition`` exists
only to fail earlier with a clearer error. Every (previous, next) pair below is
decided by both, and the verdicts must agree. The one deliberate difference:
code reads "no decision recorded" as HALTED, while the trigger accepts any
first row (the migration carried existing pauses over) -- there Python may only
be stricter.
"""
from __future__ import annotations

import itertools
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.execution.safety.trading_state import (
    CAUSES_BY_STATE,
    IllegalTradingTransition,
    Probation,
    check_transition,
    read_current,
)

pytestmark = pytest.mark.integration

STATES = ("ACTIVE", "REDUCING", "HALTED")
CAUSES = ("operator", "kill_switch", "auto", "material_deploy")
PROBATION = Probation.starting(multiplier=Decimal("0.25"), started_at_ms=1, floor={"fUST": Decimal("1")})
# Histories that set up each "previous" situation; the last row is the current state.
HISTORIES = {
    "none": [],
    **{f"{state}/{cause}": [("ACTIVE", "operator", False), (state, cause, False)]
       for state, causes in CAUSES_BY_STATE.items() for cause in causes
       if not (state == "ACTIVE" and cause == "auto")},
    "ACTIVE/in-probation": [("ACTIVE", "operator", False), ("ACTIVE", "operator", True)],
    "REDUCING/after-unpassed-probation": [("ACTIVE", "operator", False), ("ACTIVE", "operator", True),
                                          ("REDUCING", "operator", False)],
    "ACTIVE/lifted": [("ACTIVE", "operator", False), ("ACTIVE", "operator", True),
                      ("ACTIVE", "auto", False)],
}
NEXT = [(state, cause, with_probation) for state, cause in itertools.product(STATES, CAUSES)
        for with_probation in ((False, True) if state == "ACTIVE" else (False,))]

_INSERT = text("""INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause,
    actor, reason, created_at_ms, probation_multiplier, probation_started_at_ms, probation_floor)
    VALUES (:a, :env, :state, :cause, 'test', 'test', 1, :m, :s, CAST(:f AS jsonb))""")


def _params(account, env, state, cause, with_probation):
    return {"a": account, "env": env, "state": state, "cause": cause,
            "m": PROBATION.multiplier if with_probation else None,
            "s": PROBATION.started_at_ms if with_probation else None,
            "f": '{"fUST": "1"}' if with_probation else None}


@pytest.mark.asyncio
async def test_python_and_postgresql_refuse_the_same_transitions(migrated_db):
    factory, account = migrated_db
    disagreements = []
    for (name, history), (state, cause, with_probation) in itertools.product(HISTORIES.items(), NEXT):
        env = f"parity-{uuid4().hex[:10]}"
        async with factory.begin() as session:
            for row in history:
                await session.execute(_INSERT, _params(account, env, *row))
        async with factory() as session:
            current = await read_current(session, account_id=account, environment=env)
            try:
                await check_transition(session, account_id=account, environment=env, current=current,
                                       state=state, cause=cause, actor="test", reason="test",
                                       probation=PROBATION if with_probation else None)
                python_refuses = False
            except IllegalTradingTransition:
                python_refuses = True
        try:
            async with factory.begin() as session:
                await session.execute(_INSERT, _params(account, env, state, cause, with_probation))
            database_refuses = False
        except Exception:
            database_refuses = True
        if python_refuses != database_refuses and not (name == "none" and python_refuses):
            disagreements.append((name, state, cause, with_probation, python_refuses, database_refuses))
    assert disagreements == []
