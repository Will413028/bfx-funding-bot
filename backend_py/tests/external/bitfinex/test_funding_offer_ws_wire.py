"""Wire-format contract test for the Bitfinex funding-offer WS array.

WS sibling of test_funding_offers_wire.py. Proves the WS funding-offer array
(fos/fon/fou/foc) shares the REST layout owned by parse_funding_offer_row — the
2026-05-26 incident was a WS-vs-REST layout drift.

  - test_ws_funding_offer_fixture_parses_if_present: fast; replays a captured
    golden through the shared parser (skips until a golden exists).
  - test_ws_funding_offer_layout_contract: gated (@pytest.mark.integration);
    connects the real auth WS, captures fos/fon/foc offer rows, validates each
    through parse_funding_offer_row. fos (snapshot) arrives free on connect for
    any account with active funding offers — no fill needed. BFX_UPDATE_FIXTURE=1
    writes the captured raw frames as the golden.

    Capture run:
        BFX_API_KEY=... BFX_API_SECRET=... BFX_UPDATE_FIXTURE=1 \\
          uv run pytest -m integration -k ws_funding_offer --capture=no
"""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest
import websockets

from bfx_funding_bot.external.bitfinex.auth_ws import build_auth_payload
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row

_FIXTURE = Path(__file__).parent / "fixtures" / "funding_offer_ws_real.json"
_FUNDING_OFFER_MSG_TYPES = {"fos", "fon", "fou", "foc"}


def _extract_offer_rows(msg: object) -> list[list]:
    """A funding-offer channel msg is [chan, type, payload, (seq)].
    fos payload is a list of offer arrays; fon/fou/foc payload is one offer array.
    """
    if (not isinstance(msg, list) or len(msg) < 3
            or msg[1] not in _FUNDING_OFFER_MSG_TYPES):
        return []
    payload = msg[2]
    if not isinstance(payload, list) or not payload:
        return []
    if isinstance(payload[0], list):   # fos snapshot: list-of-rows
        return [r for r in payload if isinstance(r, list)]
    return [payload]                   # single-offer event


def test_ws_funding_offer_fixture_parses_if_present() -> None:
    if not _FIXTURE.exists():
        pytest.skip("no captured WS golden yet (run the gated test with BFX_UPDATE_FIXTURE=1)")
    frames = json.loads(_FIXTURE.read_text())
    rows = [r for msg in frames for r in _extract_offer_rows(msg)]
    assert rows, "captured golden must contain at least one funding-offer row"
    for r in rows:
        parse_funding_offer_row(r)  # raises BitfinexShapeError on layout drift


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ws_funding_offer_layout_contract() -> None:
    key = os.environ.get("BFX_API_KEY")
    secret = os.environ.get("BFX_API_SECRET")
    if not key or not secret:
        pytest.skip("requires BFX_API_KEY / BFX_API_SECRET for the live WS round-trip")

    captured: list[list] = []
    rows: list[list] = []
    deadline = time.monotonic() + 15.0
    async with websockets.connect("wss://api.bitfinex.com/ws/2", max_size=2**20) as ws:
        await ws.send(json.dumps(build_auth_payload(
            api_key=key, api_secret=secret, nonce_ms=int(time.time() * 1000),
        )))
        while time.monotonic() < deadline and not rows:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except TimeoutError:
                continue
            msg = json.loads(raw)
            extracted = _extract_offer_rows(msg)
            if extracted:
                captured.append(msg)
                rows.extend(extracted)

    if not rows:
        pytest.skip("no active funding offers on this account during the capture window")

    for r in rows:
        parsed = parse_funding_offer_row(r)  # raises on layout drift
        assert parsed.venue_offer_id
        assert parsed.symbol
        assert parsed.status
        print(f"[ws_contract] voi={parsed.venue_offer_id} symbol={parsed.symbol} "
              f"status={parsed.status} rate={parsed.rate} period={parsed.period_days}")

    if os.environ.get("BFX_UPDATE_FIXTURE") == "1":
        _FIXTURE.write_text(json.dumps(captured, indent=2) + "\n")
        print(f"[ws_contract] wrote {_FIXTURE} ({len(captured)} frames, {len(rows)} rows)")
