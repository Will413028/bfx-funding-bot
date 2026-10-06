"""Amount fingerprints (D3a) at the command gate, over the real ledger boundary.

Every submitted amount carries a fingerprint, so an UNKNOWN can later be told from
every other offer at the venue; the gate refuses an amount without one and a
fingerprint an open commitment already holds. How the ledger's observation cycle
resolves an UNKNOWN by its fingerprint and counts foreign offers out of the managed
exposure is tested in ``test_ledger_unknown_resolver_pg`` and ``test_ledger_basis``.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.protocols import SubmittedOrder
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitAcknowledged

from .test_capital_command_boundary import (
    boundary,
    gate_stack,  # noqa: F401 - fixture re-export
    second_ready,
)
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
D = Decimal


async def test_gate_refuses_an_amount_without_a_fingerprint(gate_stack) -> None:  # noqa: F811
    rig = await boundary(gate_stack)
    async with gate_stack.factory.begin() as session:
        row = await session.get(ExecutionDecisionRow, rig.ready.decision_id)
        row.amount_usdt = D("500")
    plain = replace(rig.ready, decision=rig.ready.decision.model_copy(
        update={"offer_amount_usdt": D("500")}))
    with pytest.raises(CommandGateBlocked, match="amount_fingerprint_missing"):
        await rig.gate.submit(plain, rig.ctx)
    assert rig.venue.received == []


async def test_gate_refuses_a_fingerprint_an_open_commitment_holds(gate_stack) -> None:  # noqa: F811
    """Two acknowledged-but-unresolved submits sharing a fingerprint would make
    the venue's answer ambiguous; the second never gets an attempt."""
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)  # acknowledged, fingerprint of AMOUNT now held
    clash = await second_ready(rig, amount="199.99990500")
    with pytest.raises(CommandGateBlocked, match="amount_fingerprint_collision"):
        await rig.gate.submit(clash, rig.ctx)
    distinct = await second_ready(rig, amount="199.99990501")

    async def another_offer(ready_, ctx_, *, cid, reservation_ref):
        rig.venue.received.append(ready_)
        return SubmittedOrder(cid=cid, venue_offer_id="102", outcome=SubmitAcknowledged("102"))

    rig.venue.submit = another_offer
    await rig.gate.submit(distinct, rig.ctx)
    assert len(rig.venue.received) == 2
