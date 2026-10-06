"""Automatic protections at the command gate and the trading state, on the ledger.

Each trigger the ledger cycle raises is tested where it is raised
(``tests/modules/execution/test_ledger_cycle_effects.py``,
``tests/modules/execution/safety/test_protection.py``); here the gate and the durable
trading state show what a trip, an UNKNOWN and a persisting condition do to trading.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.execution.command_gate import CommandGateBlocked
from bfx_funding_bot.modules.execution.protocols import SubmittedOrder
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.hard_guards import ManualKillGuard
from bfx_funding_bot.modules.execution.safety.protection import (
    IDENTITY_CONFLICT,
    AutomaticProtection,
)
from bfx_funding_bot.modules.execution.safety.tables import FundingCancelAllAuditRow
from bfx_funding_bot.modules.execution.submit_outcomes import SubmitOutcomeUnknown
from bfx_funding_bot.modules.strategy import StrategyName

from .contracts.stacks import ACCOUNT
from .test_capital_command_boundary import (
    boundary,
    gate_stack,  # noqa: F401 - fixture re-export
    second_ready,
)
from .test_kill_switch import FakeVenue
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _guarded_chain(trading, protection):
    from tests.modules.execution.deployment.test_reconciler import _CapturingSink
    return SafetyGuardChain(
        guards=[ManualKillGuard(trading_state=trading, pending_stop=protection.pending_reason)],
        probe=HealthProbe(), diagnostics=_CapturingSink(), phase=Phase.LIVE,
        strategy=StrategyName.MEAN_REVERSION, cell="a30", account_id=str(ACCOUNT))


async def test_an_unknown_submit_blocks_its_symbol_but_never_halts(gate_stack) -> None:  # noqa: F811
    """D3 level 2: an ambiguous submit leaves trading ACTIVE and cancels nothing;
    the next submit on that symbol is refused until the UNKNOWN is resolved."""
    rig = await boundary(gate_stack)
    protection = AutomaticProtection()
    rig.gate._safety_evaluator = _guarded_chain(rig.halt, protection)
    rig.gate.protection = protection
    cancel_venue = FakeVenue(gate_stack.factory, ACCOUNT, {"UST": set()})
    protection.bind(rig.halt)

    async def unknown(ready_, ctx_, *, cid, reservation_ref):
        rig.venue.received.append(ready_)
        return SubmittedOrder(cid=cid, venue_offer_id=None,
            outcome=SubmitOutcomeUnknown(reason="transport_timeout", transport_started=True))

    rig.venue.submit = unknown
    result = await asyncio.wait_for(rig.gate.submit(rig.ready, rig.ctx), timeout=10)
    assert result.outcome_kind.value == "unknown"
    await protection.run_pending()
    assert protection.pending_reason() is None
    assert (await rig.halt.current()).state == "ACTIVE"
    assert cancel_venue.calls == []
    with pytest.raises(CommandGateBlocked):
        await rig.gate.submit(await second_ready(rig), rig.ctx)
    assert len(rig.venue.received) == 1


async def test_a_trip_blocks_the_next_submit_before_halted_is_written(gate_stack) -> None:  # noqa: F811
    rig = await boundary(gate_stack)
    protection = AutomaticProtection()
    rig.gate._safety_evaluator = _guarded_chain(rig.halt, protection)
    protection.trip("loss_limiter", "realized_loss_pct_24h[fUST]=6 > 5")
    assert (await rig.halt.current()).state == "ACTIVE"  # nothing durable yet
    with pytest.raises(CommandGateBlocked, match="automatic protection tripped"):
        await rig.gate.submit(await second_ready(rig), rig.ctx)
    assert rig.venue.received == []


async def test_a_persisting_condition_writes_one_halt_and_never_the_cancel_all(gate_stack) -> None:  # noqa: F811
    """A condition that re-trips every reconcile tick: only the first tick writes HALTED.
    No protection calls the venue: the planner pulls managed offers, and the venue
    cancel-all is the operator's kill alone (D3)."""
    rig = await boundary(gate_stack)
    venue = FakeVenue(gate_stack.factory, ACCOUNT, {"UST": {"101"}})
    protection = AutomaticProtection()
    protection.bind(rig.halt)
    for tick in range(3):
        protection.trip(IDENTITY_CONFLICT, f"attempt_decision_conflict tick={tick}")
        await protection.run_pending()
    async with gate_stack.factory() as session:
        rows = (await session.scalars(select(FundingCancelAllAuditRow))).all()
    assert venue.calls == [] and rows == []  # never the venue cancel-all
    halts = [h for h in await rig.halt.history(limit=10) if h.state == "HALTED"]
    assert len(halts) == 1  # the first tick only
    assert protection.persisting == 2
    assert protection.pending_reason() is None
    state = await rig.halt.current()
    assert (state.state, state.cause, state.actor) == ("HALTED", "auto", "auto:identity_conflict")
