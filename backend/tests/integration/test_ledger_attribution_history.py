"""S1-6: the weekly attribution across the legacy -> ledger switch.

Pre-switch data keeps its numbers once the seed has run and the epoch flipped (the legacy
projections stay readable; the ledger holds the seeded provenance of the same offers), and an
offer a ledger process places after the switch, which exists only in the journal, is
attributed to its cell instead of 'unattributed'.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text

from bfx_funding_bot.modules.execution.event_store.tables import OfferClaimRow
from bfx_funding_bot.modules.live_validation.attribution_loader import (
    AttributionResult,
    load_and_compute,
)
from bfx_funding_bot.modules.live_validation.tables import (
    FundingCreditHistoryRow,
    FundingTradeRow,
)

from .bot_e2e import CELL, SCOPE, T0, BotEnv, bot_env, ledger_db  # noqa: F401 - fixtures
from .legacy_attribution_links import materialize
from .seed_e2e import (
    CELL_B,
    acked,
    boot_as_epoch,
    flip_epoch,
    run_seed,
    seed_command,
    submit,
)
from .test_ledger_schema_roles import ledger_db as roles_db  # noqa: F401 - fixture
from .test_ledger_seed_e2e import (
    FILLED,
    FILLED_AT,
    RUNNER_AT,
    SEED_AT,
    halt_moves,
    run_legacy,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ACCOUNT = str(SCOPE.exchange_account_id)
NOW = T0 + 3 * 86_400_000
RATE = Decimal("0.0001")
NEW_OFFER = 9001
NEW_AMOUNT = Decimal("210.00000111")
NEW_AT = RUNNER_AT + 20_000


@pytest.fixture
def authority() -> str:
    return "legacy"


def _history(credit_id: int, amount: Decimal, opening: int, closed: int) -> FundingCreditHistoryRow:
    return FundingCreditHistoryRow(
        exchange_account_id=SCOPE.exchange_account_id, kind="credit", credit_id=credit_id,
        deployment_environment="ci", symbol="fUST", side=1, mts_create=opening,
        mts_update=closed, amount=amount, status="CLOSED", rate=RATE, period_days=2,
        mts_opening=opening, mts_last_payout=closed,
    )


def _trade(trade_id: int, offer_id: int, amount: Decimal, at: int) -> FundingTradeRow:
    return FundingTradeRow(
        exchange_account_id=SCOPE.exchange_account_id, trade_id=trade_id,
        deployment_environment="ci", symbol="fUST", mts_create=at, offer_id=offer_id,
        amount=amount, rate=RATE, period_days=2, maker=True)


async def weekly(env: BotEnv) -> AttributionResult:
    return await load_and_compute(env.factory, account_id=ACCOUNT, deployment_environment="ci",
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


async def test_weekly_is_unchanged_by_the_switch_and_attributes_journal_only_offers(
    bot_env: BotEnv, ledger_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,  # noqa: F811
) -> None:
    env = bot_env
    await run_legacy(env, unknown=False)
    # The legacy rows exist now; the weekly reads them through the migration's copy (on the VM
    # the copy ran after the switch froze them).
    await materialize(env.factory)
    async with env.factory.begin() as session:
        session.add_all([
            # 7004's credit, ended: trade 9004 (inserted by run_legacy) -> offer 7004 -> CELL
            _history(8003, FILLED, FILLED_AT, FILLED_AT + 60_000),
            # a credit with no funding trade, and one whose trade's offer is not ours
            _history(8801, Decimal("50"), T0 + 20_000, T0 + 80_000),
            _history(8802, Decimal("70"), T0 + 30_000, T0 + 90_000),
            _trade(9802, 99_999, Decimal("70"), T0 + 30_000),
        ])
    before = numbers(await weekly(env))
    assert "8802" in before["foreign_offer"] and "8801" in before["without_trade"]
    assert any(cell == CELL and fills == 1 for cell, _w, fills, *_ in before["rows"])

    code, lines = await run_seed(seed_command(env, tmp_path, url=ledger_db.url), now_ms=SEED_AT)
    assert code == 0, lines
    await flip_epoch(env, at=SEED_AT + 1_000)
    # The seeded journal now also knows the pre-switch offers, with legacy provenance: the
    # same offers map to the same cells, no conflict, and the numbers do not move.
    assert numbers(await weekly(env)) == before

    boot_as_epoch(env)
    halt_moves(env)
    ledger = await env.build()
    await env.boot(ledger, RUNNER_AT)
    env.clock.now = RUNNER_AT + 10_000
    await submit(env, ledger, NEW_AMOUNT, acked(NEW_OFFER), cell=CELL_B)
    async with env.factory.begin() as session:
        legacy_claims = await session.scalar(select(func.count()).select_from(OfferClaimRow).where(
            OfferClaimRow.venue_offer_id == str(NEW_OFFER)))
        session.add_all([_trade(9901, NEW_OFFER, NEW_AMOUNT, NEW_AT),
                         _history(9902, NEW_AMOUNT, NEW_AT, NEW_AT + 60_000)])
    assert legacy_claims == 0  # post-switch: the journal is the only record of the offer

    after = await weekly(env)
    assert after.offer_conflicts == ()
    assert after.cells is not None and str(NEW_OFFER) not in after.cells.foreign_offer
    assert after.cells.cell_by_credit["9902"] == CELL_B
    assert set(after.cells.foreign_offer) == set(before["foreign_offer"])
    assert any(r.cell == CELL_B and r.n_fills >= 1 for r in after.rows)
    assert after.cells.cell_by_credit["8003"] == CELL


def test_the_weekly_job_role_reads_the_ledger_columns_the_port_selects(roles_db: Any) -> None:  # noqa: F811
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
    with roles_db.connect() as conn:
        for table, columns in reads.items():
            for column in columns:
                assert conn.scalar(
                    text("SELECT has_column_privilege('bfx_bot', :t, :c, 'SELECT')"),
                    {"t": table, "c": column},
                ), (table, column)
