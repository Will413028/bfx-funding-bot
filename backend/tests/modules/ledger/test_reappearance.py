"""Reappeared identities must never acquire R6 auto-close semantics."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.ledger import Scope
from bfx_funding_bot.modules.ledger._internal import observation


@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.asyncio
async def test_reappearance_never_merges_into_r6_quarantine(monkeypatch, plain):
    r6, normal = uuid4(), uuid4()
    openings = [SimpleNamespace(quarantine_id=r6, source_attempt_id=uuid4())]
    if plain:
        openings.append(SimpleNamespace(quarantine_id=normal, source_attempt_id=None))
    monkeypatch.setattr(observation, "previous_basis", AsyncMock(return_value=None))
    monkeypatch.setattr(observation, "unresolved_quarantines", AsyncMock(return_value=openings))
    opened, member = AsyncMock(), AsyncMock()
    monkeypatch.setattr(observation, "open_quarantine", opened)
    monkeypatch.setattr(observation, "add_quarantine_member", member)
    session = AsyncMock()
    session.scalar.return_value = None
    await observation._quarantine_reappearance(
        session, Scope(uuid4(), "ci"), uuid4(), "offer", "returned", "fUST", Decimal("10"), 123,
    )
    added = member.await_args.args[2]
    assert added.quarantine_id != r6
    if plain:
        opened.assert_not_awaited()
        assert added.quarantine_id == normal
    else:
        opened.assert_awaited_once()
        assert opened.await_args.args[2].quarantine_id == added.quarantine_id
