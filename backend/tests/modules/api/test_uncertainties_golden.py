"""Byte-level parity pin for the legacy uncertainty endpoints.

The golden file was captured from the legacy implementation before the
read-model port existed; the legacy read model must keep producing it.
Regenerate only deliberately: ``UPDATE_UNCERTAINTY_GOLDEN=1 pytest <this file>``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from decimal import Decimal
from pathlib import Path

import bfx_funding_bot.modules.ledger.tables  # noqa: F401
from bfx_funding_bot.modules.execution.events import VenueOfferObservation
from tests.modules.api.test_uncertainties_router import (
    ACCOUNT_ID,
    _append_snapshot,
    _apply_queued,
    _mark_not_accepted,
    _seed_orphan_uncertainty,
)
from tests.modules.api.test_uncertainties_router import (
    uncertainty_app as uncertainty_app,
)

GOLDEN = Path(__file__).parent / "golden" / "uncertainties_legacy.json"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _canonical(value: object, uncertainty_id: str) -> str:
    text = json.dumps(value, sort_keys=True, indent=2)
    text = text.replace(uncertainty_id, "<UNCERTAINTY>")
    return _UUID.sub("<UUID>", text)


def _scenario(client, factory, *, applied: bool) -> dict[str, object]:
    uid = str(client.uncertainty_id)  # type: ignore[attr-defined]
    base = f"/api/v1/exchange-accounts/{ACCOUNT_ID}"
    reconcile_seq = asyncio.run(_append_snapshot(factory, finished_at=2_000))
    out: dict[str, object] = {}
    out["list_before_request"] = client.get(f"{base}/uncertainties").json()
    queued = _mark_not_accepted(client, reconcile_seq)
    assert queued.status_code == 202, queued.text
    request_id = queued.json()["data"]["requestId"]
    if applied:
        assert _apply_queued(factory) is True
    out["list_all"] = client.get(f"{base}/uncertainties").json()
    out["list_open"] = client.get(f"{base}/uncertainties", params={"state": "open"}).json()
    out["list_resolved"] = client.get(
        f"{base}/uncertainties", params={"state": "resolved"}
    ).json()
    out["detail"] = client.get(f"{base}/uncertainties/{uid}").json()
    request = client.get(f"{base}/uncertainty-resolution-requests/{request_id}").json()
    request["data"]["createdAtMs"] = "<ms>"
    out["request"] = request
    for rows in ("list_all", "list_open", "list_resolved"):
        for item in out[rows]["data"]:  # type: ignore[index]
            if item.get("resolutionRequest"):
                item["resolutionRequest"]["createdAtMs"] = "<ms>"
    if out["detail"]["data"].get("resolutionRequest"):  # type: ignore[index]
        out["detail"]["data"]["resolutionRequest"]["createdAtMs"] = "<ms>"  # type: ignore[index]
    out["_uid"] = uid
    return out


def _check(name: str, scenario: dict[str, object]) -> None:
    uid = str(scenario.pop("_uid"))
    actual = _canonical(scenario, uid)
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.environ.get("UPDATE_UNCERTAINTY_GOLDEN") == "1":
        golden[name] = json.loads(actual)
        GOLDEN.write_text(json.dumps(golden, sort_keys=True, indent=2) + "\n")
    assert actual == json.dumps(golden[name], sort_keys=True, indent=2)


def test_legacy_open_uncertainty_endpoints_match_golden(uncertainty_app) -> None:
    client, factory = uncertainty_app
    _check("open", _scenario(client, factory, applied=False))


def test_legacy_resolved_uncertainty_endpoints_match_golden(uncertainty_app) -> None:
    client, factory = uncertainty_app
    _check("resolved", _scenario(client, factory, applied=True))


def test_legacy_orphan_uncertainty_endpoints_match_golden(uncertainty_app) -> None:
    client, factory = uncertainty_app
    offer = VenueOfferObservation(
        venue_offer_id="venue-golden-orphan",
        symbol="fUSD",
        amount_original=Decimal("250.5"),
        amount_remaining=Decimal("250.5"),
        rate=Decimal("0.0004"),
        period_days=2,
        status="active",
        mts_created=1_500,
        mts_updated=1_500,
        offer_type="LIMIT",
        flags={"raw": 0},
    )
    orphan_id = str(asyncio.run(_seed_orphan_uncertainty(factory, offer)))
    base = f"/api/v1/exchange-accounts/{ACCOUNT_ID}"
    out: dict[str, object] = {
        "list_all": client.get(f"{base}/uncertainties").json(),
        "detail": client.get(f"{base}/uncertainties/{orphan_id}").json(),
        "_uid": orphan_id,
    }
    _check("orphan", out)
