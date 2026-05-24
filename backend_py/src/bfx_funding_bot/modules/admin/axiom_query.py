"""AxiomSmokeQueryAdapter — APL query for smoke L3 round-trip verification.

Lightweight (3-column projection: _time, event_type, account_id) — proves
events surfaced in Axiom. Not for state reconstruction (boot uses PG from_snapshot).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


class AxiomSmokeQueryAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        dataset: str,
        base_url: str = "https://api.axiom.co",
        timeout: float = 30.0,
    ) -> None:
        self._dataset = dataset
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def query_order_events(
        self, account_id: str, since: datetime,
    ) -> list[dict[str, Any]]:
        """Return events with event_type ∈ {reservation_claimed, order_fill,
        reservation_released} for given account_id since timestamp.

        Raises httpx.HTTPStatusError on 4xx/5xx.
        """
        apl = self._build_apl(account_id, since)
        resp = await self._http.post(
            "/v1/datasets/_apl?format=tabular",
            json={"apl": apl},
        )
        resp.raise_for_status()
        return self._tabular_to_rows(resp.json())

    def _build_apl(self, account_id: str, since: datetime) -> str:
        # APL syntax: ['dataset'] | where ... | project ... | order by ...
        # _time filter uses datetime() literal; since is UTC-aware ISO format.
        return (
            f"['{self._dataset}']"
            f"\n| where ['event_type'] in ('reservation_claimed', 'order_fill', 'reservation_released')"
            f"\n  and ['account_id'] == '{account_id}'"
            f"\n  and _time > datetime({since.isoformat()})"
            f"\n| project _time, ['event_type'], ['account_id']"
            f"\n| order by _time asc"
        )

    @staticmethod
    def _tabular_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        tables = payload.get("tables") or []
        if not tables:
            return []
        t = tables[0]
        fields = [f["name"] for f in t.get("fields", [])]
        cols = t.get("columns") or []
        if not fields or not cols:
            return []
        n_rows = len(cols[0]) if cols else 0
        return [
            {fields[ci]: cols[ci][ri] for ci in range(len(fields))}
            for ri in range(n_rows)
        ]

    async def aclose(self) -> None:
        await self._http.aclose()
