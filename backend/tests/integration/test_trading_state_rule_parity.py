"""Python's trading-state pre-check refuses exactly what PostgreSQL refuses.

The insert trigger and CHECKs are the authority; ``check_transition`` exists
only to fail earlier with a clearer error. Every (previous, next) pair below is
decided by both, and the verdicts must agree. The one deliberate difference:
code reads "no decision recorded" as HALTED, while the trigger accepts any
first row -- there Python may only be stricter.
"""
from __future__ import annotations

import itertools
import time
from uuid import uuid4

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.execution.safety.trading_state import (
    IllegalTradingTransition,
    check_transition,
    count_auto_resumes,
    read_current,
)

pytestmark = pytest.mark.integration

# REDUCING, material_deploy and kill_switch are retired: both sides must refuse them.
STATES = ("ACTIVE", "HALTED", "REDUCING")
CAUSES = ("operator", "auto", "material_deploy", "kill_switch")
MIN = 60 * 1000
# Histories that set up each "previous" situation; the last row is the current
# state. Each row is (state, cause, minutes before now): the automatic-resume
# rules (8e4b2f6a1c37) depend on the halt's age and on resumes in the last day.
HISTORIES = {
    "none": [],
    "ACTIVE/operator": [("ACTIVE", "operator", 60)],
    "ACTIVE/operator-after-halt": [("ACTIVE", "operator", 60), ("HALTED", "auto", 50),
                                   ("ACTIVE", "operator", 40)],
    "HALTED/operator": [("ACTIVE", "operator", 60), ("HALTED", "operator", 30)],
    "HALTED/auto": [("ACTIVE", "operator", 60), ("HALTED", "auto", 30)],
    "HALTED/auto-young": [("ACTIVE", "operator", 60), ("HALTED", "auto", 5)],
    "HALTED/auto-after-two-resumes": [
        ("ACTIVE", "operator", 600), ("HALTED", "auto", 500), ("ACTIVE", "auto", 480),
        ("HALTED", "auto", 400), ("ACTIVE", "auto", 380), ("HALTED", "auto", 30)],
    "HALTED/auto-resumes-a-day-ago": [
        ("ACTIVE", "operator", 3000), ("HALTED", "auto", 2000), ("ACTIVE", "auto", 1980),
        ("HALTED", "auto", 1900), ("ACTIVE", "auto", 1880), ("HALTED", "auto", 30)],
    "ACTIVE/auto": [("ACTIVE", "operator", 60), ("HALTED", "auto", 50), ("ACTIVE", "auto", 30)],
}
NEXT = list(itertools.product(STATES, CAUSES))

# History is fixture data with chosen ages: the trigger would stamp automatic rows
# with the database clock, so it is written with triggers off (id drawn as the
# trigger would). Only the transition under test goes through the trigger.
_HISTORY = text("""INSERT INTO trading_state (id, exchange_account_id, deployment_environment, state,
    cause, actor, reason, created_at_ms) VALUES (nextval('trading_state_id_seq'), :a, :env, :state,
    :cause, 'test', 'test', :at)""")
_INSERT = text("""INSERT INTO trading_state (exchange_account_id, deployment_environment, state, cause,
    actor, reason, created_at_ms) VALUES (:a, :env, :state, :cause, 'test', 'test', :at)""")


@pytest.mark.asyncio
async def test_python_and_postgresql_refuse_the_same_transitions(migrated_db):
    factory, account = migrated_db
    disagreements = []
    database_verdicts = {}
    for (name, history), (state, cause) in itertools.product(HISTORIES.items(), NEXT):
        env = f"parity-{uuid4().hex[:10]}"
        now_ms = int(time.time() * 1000)
        async with factory.begin() as session:
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            for row_state, row_cause, minutes_ago in history:
                await session.execute(_HISTORY, {"a": account, "env": env, "state": row_state,
                                                "cause": row_cause, "at": now_ms - minutes_ago * MIN})
        async with factory() as session:
            current = await read_current(session, account_id=account, environment=env)
            resumes = await count_auto_resumes(session, account_id=account, environment=env,
                                               now_ms=now_ms)
        try:
            await check_transition(current=current, state=state, cause=cause, actor="test",
                                   reason="test", now_ms=now_ms, auto_resumes_in_window=resumes)
            python_refuses = False
        except IllegalTradingTransition:
            python_refuses = True
        try:
            async with factory.begin() as session:
                await session.execute(_INSERT, {"a": account, "env": env, "state": state,
                                                "cause": cause, "at": now_ms})
            database_refuses = False
        except Exception:
            database_refuses = True
        database_verdicts[name, state, cause] = database_refuses
        if python_refuses != database_refuses and not (name == "none" and python_refuses):
            disagreements.append((name, state, cause, python_refuses, database_refuses))
    assert disagreements == []
    # Agreement is not enough: pin who may end an automatic halt.
    resume = {name: database_verdicts[name, "ACTIVE", "auto"] for name in HISTORIES}
    assert {name for name, refused in resume.items() if not refused} == {
        "HALTED/auto", "HALTED/auto-resumes-a-day-ago"}
    assert not database_verdicts["HALTED/operator", "ACTIVE", "operator"]
