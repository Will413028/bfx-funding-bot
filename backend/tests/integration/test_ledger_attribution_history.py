"""S1-6: the weekly attribution of an offer only the ledger journal knows.

An offer the ledger places exists only in the journal (no legacy claim); it is attributed to
its cell instead of 'unattributed'. (How pre-switch data kept its numbers across the switch
was checked by booting the legacy runtime, which S1-8 removed.)
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from bfx_funding_bot.modules.live_validation.attribution_loader import (
    AttributionResult,
    load_and_compute,
)
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingTradeRow,
)

from .contracts.stacks import ACCOUNT as ACCOUNT_ID
from .contracts.stacks import NOW as PLACED_AT
from .test_capital_command_boundary import (
    boundary,
    gate_stack,  # noqa: F401 - fixture re-export
)
from .test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ACCOUNT = str(ACCOUNT_ID)
T0 = PLACED_AT
NOW = T0 + 3 * 86_400_000
RATE = Decimal("0.0001")
OFFER = 101  # what the rig's venue acknowledges
CELL = "a30"


def _history(credit_id: int, amount: Decimal, opening: int, closed: int) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=ACCOUNT_ID, kind="credit", credit_id=credit_id,
        deployment_environment="ci", symbol="fUST", side=1, mts_create=opening,
        mts_update=closed, amount=amount, status="CLOSED", rate=RATE, period_days=2,
        mts_opening=opening, mts_last_payout=closed,
    )


def _trade(trade_id: int, offer_id: int, amount: Decimal, at: int) -> FundingTradeRow:
    return FundingTradeRow(
        exchange_account_id=ACCOUNT_ID, trade_id=trade_id,
        deployment_environment="ci", symbol="fUST", mts_create=at, offer_id=offer_id,
        amount=amount, rate=RATE, period_days=2, maker=True)


async def weekly(factory: Any) -> AttributionResult:
    return await load_and_compute(factory, account_id=ACCOUNT, deployment_environment="ci",
                                  now_ms=NOW)


def numbers(result: AttributionResult) -> dict[str, Any]:
    """What the weekly reports for the data: per-cell rows and the diagnostic counts."""
    assert result.cells is not None
    return {
        "rows": sorted((r.cell, r.week_start_ms, r.n_fills, r.gross_interest_usdt,
                        r.net_interest_usdt, r.capital_days) for r in result.rows),
        "credits": result.credits,
        "foreign_offer": sorted(result.cells.foreign_offer),
        "without_trade": sorted(result.cells.without_trade),
        "conflicts": result.offer_conflicts,
    }


async def test_an_offer_only_the_journal_knows_is_attributed_to_its_cell(gate_stack) -> None:  # noqa: F811
    """No legacy claim exists for an offer the ledger placed: the journal alone names its cell."""
    rig = await boundary(gate_stack)
    await rig.gate.submit(rig.ready, rig.ctx)  # acknowledged as offer 101, cell a30
    amount = rig.ready.decision.offer_amount_usdt
    async with gate_stack.factory.begin() as session:
        session.add_all([
            _trade(9901, OFFER, amount, T0 + 100),
            _history(9902, amount, T0 + 100, T0 + 60_000),
            # a credit with no funding trade, and one whose trade's offer is not ours
            _history(8801, Decimal("50"), T0 + 200, T0 + 80_000),
            _history(8802, Decimal("70"), T0 + 300, T0 + 90_000),
            _trade(9802, 99_999, Decimal("70"), T0 + 300),
        ])

    result = await weekly(gate_stack.factory)
    assert result.offer_conflicts == ()
    assert result.cells is not None and str(OFFER) not in result.cells.foreign_offer
    assert result.cells.cell_by_credit["9902"] == CELL
    assert "8802" in result.cells.foreign_offer and "8801" in result.cells.without_trade
    assert any(r.cell == CELL and r.n_fills >= 1 for r in result.rows)


def test_the_weekly_job_role_reads_the_ledger_columns_the_port_selects(ledger_db: Any) -> None:  # noqa: F811
    """The weekly connects as ``bfx_bot``: the port's columns are granted, no migration needed."""
    reads = {
        "submission_attempt_journal": ("exchange_account_id", "deployment_environment", "cell_id",
                                       "execution_decision_id", "attempt_seq", "seed_provenance",
                                       "attempt_id"),
        "transport_outcome_journal": ("attempt_id", "kind", "venue_offer_id"),
        "venue_credit_mirror": ("exchange_account_id", "deployment_environment",
                                "venue_credit_id", "source_kind", "symbol", "amount", "rate",
                                "period_days", "mts_created", "mts_opening", "terminal_kind",
                                "present_in_latest_accepted_snapshot"),
    }
    with ledger_db.connect() as conn:
        for table, columns in reads.items():
            for column in columns:
                assert conn.scalar(
                    text("SELECT has_column_privilege('bfx_bot', :t, :c, 'SELECT')"),
                    {"t": table, "c": column},
                ), (table, column)
