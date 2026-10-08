"""A revision that applies an operator request carries no origin of its own.

Who asked and why are the request's columns, reached through the revision's typed
``operator_request_id``; copying them into ``source`` would keep a second, unconstrained
version of the same fact (ADR 2026-10-08 operator-requests-keep-state-effects-carry-request-id,
Amendment 2026-10-09).
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.accounts.capital_amendment import PolicyChanges, amend_capital_policy


@pytest.mark.asyncio
async def test_an_origin_beside_an_operator_request_is_refused_before_anything_is_read() -> None:
    class Untouched:
        def __getattr__(self, name: str) -> Any:
            pytest.fail(f"touched {name}")

    with pytest.raises(ValueError, match="no origin of its own"):
        await amend_capital_policy(
            Untouched(), store=Untouched(), scope_lock=Untouched(), symbol="fUST",
            changes=PolicyChanges(enabled=False), apply_digest="d",
            origin={"reason": "copied"}, operator_request_id=uuid4())
