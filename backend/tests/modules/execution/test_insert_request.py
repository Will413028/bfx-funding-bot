"""``insert_request``: only a taken pending slot is refused quietly, on either dialect.

The pending slot is the model's partial unique index (``pending_index``); the insert names it
as the ``ON CONFLICT`` arbiter. Every other refusal -- a CHECK, a duplicate id -- raises. The
PostgreSQL side is ``tests/modules/execution/test_operator_requests.py``.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Index
from sqlalchemy.exc import IntegrityError

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.operator_requests import insert_request
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
from bfx_funding_bot.modules.execution.uncertainty_tables import UncertaintyResolutionRequestRow

_ACCOUNT = uuid4()


def _trading(**extra: object) -> dict[str, object]:
    return {"request_id": uuid4(), "exchange_account_id": _ACCOUNT, "deployment_environment": "ci",
            "action": "resume", "reason": "test", "requested_by": "operator",
            "created_at_ms": 1, **extra}


@pytest.fixture
async def session(sqlite_engine: Any, sqlite_session: Any) -> Any:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[TradingControlRequestRow.__table__])
    return sqlite_session


@pytest.mark.parametrize(("model", "values"), [
    (TradingControlRequestRow, {"action": "resume"}),
    (TradingControlRequestRow, {"action": "kill"}),
    (CapitalPolicyRequestRow, {}),
    (UncertaintyResolutionRequestRow, {}),
])
def test_the_pending_slot_is_a_partial_unique_index_on_both_dialects(model: Any, values: Any) -> None:
    name = model.pending_index(values)
    index = next(index for index in model.__table__.indexes if index.name == name)
    assert isinstance(index, Index) and index.unique
    where = {dialect: str(index.dialect_options[dialect]["where"])
             for dialect in ("postgresql", "sqlite")}
    assert "state = 'requested'" in where["postgresql"]
    assert where["postgresql"] == where["sqlite"]


@pytest.mark.asyncio
async def test_a_taken_slot_is_false_and_a_kill_has_its_own(session: Any) -> None:
    assert await insert_request(session, TradingControlRequestRow, _trading())
    assert not await insert_request(session, TradingControlRequestRow, _trading())
    assert await insert_request(session, TradingControlRequestRow, _trading(action="kill"))
    assert not await insert_request(session, TradingControlRequestRow, _trading(action="kill"))


@pytest.mark.asyncio
async def test_a_refusal_other_than_the_slot_raises(session: Any) -> None:
    taken = _trading()
    assert await insert_request(session, TradingControlRequestRow, taken)
    # A duplicate id is not a taken slot, even with the kill slot free.
    with pytest.raises(IntegrityError):
        await insert_request(session, TradingControlRequestRow,
                             _trading(request_id=taken["request_id"], action="kill"))
    await session.rollback()
    # A blank reason breaks the CHECK; it was once reported as a pending request.
    with pytest.raises(IntegrityError):
        await insert_request(session, TradingControlRequestRow, _trading(reason="   "))
