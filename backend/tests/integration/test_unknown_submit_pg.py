"""The venue's offer-history transport the ledger observation reads.

How the ledger resolves an UNKNOWN from an observation is tested in
``test_ledger_unknown_resolver_pg``; the legacy event store's UNKNOWN regressions went with
the store (S1-8 PR-D).
"""
from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials

pytestmark = pytest.mark.integration

_ACCOUNT = UUID("00000000-0000-0000-0000-000000000041")


def _wire_offer(offer_id: int, mts_created: int) -> list[object | None]:
    row: list[object | None] = [None] * 21
    row[0] = offer_id
    row[1] = "fUST"
    row[2] = mts_created
    row[3] = mts_created
    row[4] = -12.5
    row[5] = -12.5
    row[6] = "LIMIT"
    row[9] = 0
    row[10] = "CANCELED"
    row[14] = 0.00031
    row[15] = 2
    return row


@pytest.mark.asyncio
async def test_history_transport_pages_backward_and_records_complete_coverage_fence():
    """Changing the cursor/end fence must make pagination coverage observable."""
    bodies: list[dict[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json=[_wire_offer(3, 3_000), _wire_offer(2, 2_000)])
        return httpx.Response(200, json=[_wire_offer(1, 1_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            auth_gate=AuthRequestGate(iter((1, 2)).__next__),
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
            ),
            start_ms=1_000,
            end_ms=5_000,
            limit=2,
        )

    assert bodies == [
        {"start": 1_000, "end": 5_000, "limit": 2},
        {"start": 1_000, "end": 1_999, "limit": 2},
    ]
    assert [offer.venue_offer_id for offer in result.offers] == ["1", "2", "3"]
    assert result.coverage.complete is True
    assert result.coverage.oldest_mts_created == 1_000
    assert result.coverage.newest_mts_created == 3_000
    assert result.coverage.pages == 2


@pytest.mark.asyncio
async def test_history_row_stamped_before_the_fence_keeps_a_short_page_complete():
    """2026-09-29: the venue stamps offers to the whole second, so a window
    starting at .175 returns an offer created at .000. Marking that incomplete
    made the fill unclassifiable and halted trading."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_wire_offer(1, 1_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            auth_gate=AuthRequestGate(iter((1,)).__next__),
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
            ),
            start_ms=1_175,
            end_ms=5_000,
            limit=2,
        )

    assert result.coverage.complete is True
    assert result.coverage.oldest_mts_created == 1_000


@pytest.mark.asyncio
async def test_full_history_page_at_start_fence_remains_incomplete():
    """A full boundary page may hide more rows sharing the oldest timestamp."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_wire_offer(2, 1_000), _wire_offer(1, 1_000)],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await BitfinexAuthREST(
            http=http,
            auth_gate=AuthRequestGate(iter((1,)).__next__),
        ).get_funding_offer_history(
            ctx=AccountContext(
                account_id=str(_ACCOUNT),
                credentials=Credentials(api_key="key", api_secret="secret"),
            ),
            start_ms=1_000,
            end_ms=5_000,
            limit=2,
        )

    assert result.coverage.complete is False
    assert result.coverage.oldest_mts_created == 1_000
    assert result.coverage.pages == 1
