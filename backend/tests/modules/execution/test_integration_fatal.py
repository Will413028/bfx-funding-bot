"""Path E — ExecutorAuthError propagates → daemon._run sys.exit(78).

Subprocess end-to-end coverage lives in
tests/integration/test_path_e_subprocess.py (needs testcontainer Postgres).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from bfx_funding_bot.core.errors import (
    EXIT_CODE_AUTH_FAILED,
    ExecutorAuthError,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
)

pytestmark = pytest.mark.integration


class _AuthFailExecutor:
    async def submit(
        self, d: DecisionPayload, c: AccountContext,
    ) -> SubmittedOrder:
        raise ExecutorAuthError("401")


@pytest.mark.asyncio
async def test_executor_auth_error_is_distinct_exception_class() -> None:
    ex = _AuthFailExecutor()
    decision = DecisionPayload(
        decision_outcome=DecisionOutcome.POST,
        signal_correlation_id=uuid4(),
        offer_rate=0.0001,
        offer_amount_usdt=100.0,
        offer_duration_days=2,
    symbol="fUST")
    ctx = AccountContext("default", Credentials("k", "s"), Decimal("500"))
    with pytest.raises(ExecutorAuthError):
        await ex.submit(decision, ctx)


def test_exit_code_constant() -> None:
    assert EXIT_CODE_AUTH_FAILED == 78
