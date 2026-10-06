"""G3 capital budget C: explicit --capital, else the funding-wallet balance
from the venue ledger. Regression for the 2026-09-27 production weekly run:
BFX_ALLOCATION_CAP_USDT is 0 since the capital policy replaced allocation caps,
the old env default fed C=0, and every window raised."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.uncertainty_tables  # the ledger tables' FK target
import bfx_funding_bot.modules.live_validation.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.external.bitfinex.auth_rest import InterestPayment
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.live_validation.g3 import build_g3_report
from bfx_funding_bot.modules.live_validation.interest_ledger import wallet_balance_basis
from bfx_funding_bot.modules.live_validation.live_attribution import VerdictState
from bfx_funding_bot.modules.live_validation.tables import (
    AttributionLegacyOfferCellRow,
    FundingCreditHistoryRow,
    FundingInterestPaymentRow,
    FundingTradeRow,
)
from scripts.run_g3_live_validation import _parse_capital

ACCOUNT = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
DAY = 86_400_000
DESC = "Margin Funding Payment on wallet funding"


def test_explicit_capital_overrides_and_must_be_positive():
    assert _parse_capital("10000") == Decimal("10000")
    assert _parse_capital(None) is None
    with pytest.raises(SystemExit):
        _parse_capital("0")


def _pay(ledger_id: int, mts: int, amount: str, balance: str) -> InterestPayment:
    return InterestPayment(ledger_id, "UST", "funding", mts, Decimal(amount), Decimal(balance), DESC)


def test_wallet_balance_basis_per_window():
    # live payouts 2026-09-25..27 (balance after each payout)
    payments = [_pay(1, 1790299821000, "0.01948396", "395.51441399"),
                _pay(2, 1790386231000, "0.00213578", "395.51654977"),
                _pay(3, 1790472624000, "0.0518895", "395.56843927")]
    basis = wallet_balance_basis(payments, currency="UST")
    assert basis is not None
    # inside: mean balance before each payout
    assert basis(1790299821000, 1790386231001) == (
        Decimal("395.49493003") + Decimal("395.51441399")) / 2
    # after the last payout: its resulting balance; before the first: the balance it started from
    assert basis(1790500000000, 1790600000000) == Decimal("395.56843927")
    assert basis(0, 1000) == Decimal("395.49493003")
    assert wallet_balance_basis([], currency="UST") is None


@pytest_asyncio.fixture
async def sf(sqlite_engine: AsyncEngine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


def _credit(credit_id: int, opened: int, closed: int) -> FundingCreditHistoryRow:
    # 150.76884612 fUST at 0.00019999, the live account's 2026-09-25 credit size
    return FundingCreditHistoryRow(
        exchange_account_id=ACCOUNT, kind="credit", credit_id=credit_id,
        deployment_environment="prod", symbol="fUST", side=1, mts_create=opened,
        mts_update=opened, amount=Decimal("150.76884612"), status="CLOSED",
        rate=Decimal("0.00019999"), period_days=2, mts_opening=opened, mts_last_payout=closed,
    )


def _ours(credit_id: int, opened: int) -> list[object]:
    """Trade → our offer → execution decision, so the credit is the bot's."""
    return [
        FundingTradeRow(
            exchange_account_id=ACCOUNT, trade_id=credit_id, deployment_environment="prod",
            symbol="fUST", mts_create=opened, offer_id=credit_id, amount=Decimal("150.76884612"),
            rate=Decimal("0.00019999"), period_days=2, maker=None,
        ),
        AttributionLegacyOfferCellRow(
            exchange_account_id=ACCOUNT, deployment_environment="prod",
            venue_offer_id=str(credit_id), cell="fUST_p2",
        ),
        ExecutionDecisionRow(
            decision_id=f"d{credit_id}", account_id=str(ACCOUNT), exchange_account_id=ACCOUNT,
            deployment_environment="prod", reconcile_id="r", cell_id="fUST_p2", symbol="fUST",
            signal_correlation_id=f"s{credit_id}", outcome="submitted",
            signal_rate=Decimal("0.00019999"), amount_usdt=Decimal("150.76884612"),
            duration_days=2, model_evidence={}, safety_result={}, execution_policy="p",
            service_version="v", config_hash="h", occurred_at_ms=opened, recorded_at_ms=opened,
        ),
    ]


@pytest.mark.asyncio
async def test_zero_allocation_cap_env_uses_the_ledger_balance(sf, monkeypatch):
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(ACCOUNT))
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    monkeypatch.setenv("BFX_ALLOCATION_CAP_USDT", "0")
    now = int(datetime.now(UTC).timestamp() * 1000)
    async with sf() as s:
        s.add_all([_credit(1, now - 10 * DAY, now - 8 * DAY),
                   _credit(2, now - 3 * DAY, now - DAY),
                   *_ours(1, now - 10 * DAY), *_ours(2, now - 3 * DAY)])
        s.add_all([FundingInterestPaymentRow(
            exchange_account_id=ACCOUNT, ledger_id=i, deployment_environment="prod",
            currency="UST", mts=now - i * DAY, amount=Decimal("0.05"),
            balance=Decimal("395.55"), description=DESC,
        ) for i in range(1, 12)])
        await s.commit()
    report = await build_g3_report(capital=_parse_capital(None), session_factory=sf)
    assert report.capital_source == "ledger"
    assert report.coverage.bot_credits == 2
    assert report.deployment.cap == Decimal("395.50")          # balance before each payout
    assert report.verdict.state is not None


@pytest.mark.asyncio
async def test_without_capital_or_ledger_fails_with_a_clear_message(sf, monkeypatch):
    monkeypatch.setenv("BFX_EXCHANGE_ACCOUNT_ID", str(ACCOUNT))
    monkeypatch.setenv("BFX_DEPLOYMENT_ENV", "prod")
    async with sf() as s:
        now = int(datetime.now(UTC).timestamp() * 1000)
        s.add(_credit(1, now - 2 * DAY, now - DAY))
        await s.commit()
    with pytest.raises(RuntimeError, match="--capital"):
        await build_g3_report(capital=None, session_factory=sf)
    report = await build_g3_report(capital=Decimal("570"), session_factory=sf)
    assert isinstance(report.verdict.state, VerdictState)
    assert report.capital_source == "--capital 570"
