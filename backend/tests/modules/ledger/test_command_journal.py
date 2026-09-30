"""Ack-only provenance refuses contradictory resolution and outcome identity."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from bfx_funding_bot.modules.ledger import Outcome, ProvenanceConflict, Scope
from bfx_funding_bot.modules.ledger._internal import journal, reads
from bfx_funding_bot.modules.ledger.tables import TransportOutcomeJournalRow, VenueOfferMirrorRow
from bfx_funding_bot.modules.ledger.wiring import build_command_journal


@pytest.mark.asyncio
@pytest.mark.parametrize("action,venue,symbol", [
    ("not_accepted", None, "fUST"),
    ("bound_to_venue", "another", "fUST"),
    ("bound_to_venue", "offer", "fUSD"),
])
async def test_ack_only_cancel_refuses_conflicting_resolution(monkeypatch, action, venue, symbol):
    scope, attempt_id = Scope(uuid4(), "ci"), uuid4()
    attempt = SimpleNamespace(attempt_id=attempt_id, symbol="fUST", execution_decision_id="decision", cell_id="cell")

    class Session:
        async def get(self, table, key, **kwargs):
            if table is VenueOfferMirrorRow:
                return None
            assert table is TransportOutcomeJournalRow
            return SimpleNamespace(kind="ack", venue_offer_id="offer")

        async def scalar(self, query):
            return SimpleNamespace(action=action, venue_offer_id=venue, symbol=symbol,
                                   exchange_account_id=scope.exchange_account_id, deployment_environment="ci")

    monkeypatch.setattr(reads, "_live", AsyncMock(return_value=reads._Live((), ())))
    monkeypatch.setattr(reads, "offer_provenance", AsyncMock(return_value={"offer": {attempt_id}}))
    monkeypatch.setattr(reads, "attempts_by_id", AsyncMock(return_value=[attempt]))
    monkeypatch.setattr(reads, "_correlations", AsyncMock(return_value={"decision": "correlation"}))
    with pytest.raises(ProvenanceConflict):
        await reads.cancel_provenance(Session(), scope, "offer")


@pytest.mark.asyncio
async def test_ledger_readback_refuses_mismatched_outcome_identity(monkeypatch):
    scope, attempt_id = Scope(uuid4(), "ci"), uuid4()

    class Session:
        async def get(self, table, key):
            return SimpleNamespace(exchange_account_id=scope.exchange_account_id, deployment_environment="ci")

    @asynccontextmanager
    async def factory():
        yield Session()

    monkeypatch.setattr(journal, "read_back_outcome", AsyncMock(return_value=Outcome(uuid4(), "ack", "offer", None, 10, {})))
    with pytest.raises(ValueError, match="outcome identity mismatch"):
        await build_command_journal(factory).read_back_outcome(scope, attempt_id)
