"""Builders for one funding decision passing through the execution gate.

Shared by the eligibility tests and by tests that drive the gate from elsewhere
(dependency outages in tests/modules/marketfeed).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from bfx_funding_bot.modules.execution.audit.model import AuditContext
from bfx_funding_bot.modules.execution.deployment.period_pricing import (
    PriceBranch,
    PriceDecision,
)
from bfx_funding_bot.modules.marketfeed.funding_book import MarketSnapshot
from bfx_funding_bot.modules.strategy import DecisionOutcome as PayloadOutcome
from bfx_funding_bot.modules.strategy import DecisionPayload


def make_candidate() -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=PayloadOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.00020,
        offer_amount_usdt=100.0,
        offer_duration_days=14,
        symbol="fUST",
    )


def make_audit_context(candidate: DecisionPayload) -> AuditContext:
    return AuditContext(
        account_id="acct",
        deployment_environment="test",
        reconcile_id="reconcile-1",
        cell_id="cell-1",
        symbol=candidate.symbol,
        signal_correlation_id=str(candidate.signal_correlation_id),
        service_version="test",
        config_hash="config",
    )


def make_price() -> PriceDecision:
    return PriceDecision(
        rate=Decimal("0.00021"),
        branch=PriceBranch.UNDERCUT,
        evidence={"period_days": 14},
    )


def make_snapshot(
    *,
    symbol: str = "fUST",
    sequence_valid: bool = True,
    checksum_valid: bool = True,
) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="book-1",
        symbol=symbol,
        bids=(),
        asks=(),
        captured_at_ms=1_000,
        received_at_ms=1_000,
        source="ws",
        sequence_valid=sequence_valid,
        checksum_valid=checksum_valid,
        sequence=2,
    )
