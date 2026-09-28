"""Fail-closed, account/environment/symbol-scoped uncertainty guard tests."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.safety.hard_guards import UncertaintyGuard
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload

ACCOUNT = UUID("550e8400-e29b-41d4-a716-446655440000")


def _decision(symbol: str) -> DecisionPayload:
    return DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100,
        offer_duration_days=2,
        symbol=symbol,
    )


def _context() -> AccountContext:
    return AccountContext(
        account_id=str(ACCOUNT),
        credentials=Credentials("k", "s"),
        allocation_cap_usdt=Decimal("500"),
    )


class _Reader:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error

    async def list_open(self, **_kwargs):
        if self.error:
            raise self.error
        return self.rows


@pytest.mark.asyncio
async def test_uncertainty_guard_blocks_only_matching_scope() -> None:
    guard = UncertaintyGuard(
        reader=_Reader([{"kind": "submit_outcome_unknown", "symbol": "fUST"}]),
        deployment_environment="ci",
    )

    blocked = await guard.evaluate(_decision("fUST"), _context())
    unrelated = await guard.evaluate(_decision("fUSD"), _context())

    assert blocked.allowed is False
    assert unrelated.allowed is True


@pytest.mark.asyncio
async def test_uncertainty_guard_blocks_unknown_kind_and_reader_error() -> None:
    unknown = UncertaintyGuard(
        reader=_Reader([{"kind": "future_kind", "symbol": "fUST"}]),
        deployment_environment="ci",
    )
    read_error = UncertaintyGuard(
        reader=_Reader(error=RuntimeError("db unavailable")),
        deployment_environment="ci",
    )

    assert (await unknown.evaluate(_decision("fUST"), _context())).allowed is False
    assert (await read_error.evaluate(_decision("fUST"), _context())).allowed is False
