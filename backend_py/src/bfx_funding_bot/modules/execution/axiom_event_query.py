"""AxiomReplayQueryAdapter — real Axiom APL adapter for ledger + registry replay.

Conforms to two consumer protocols (Hexagonal port-per-consumer pattern):
  - modules/execution/ledger.py:_AxiomQueryProtocol — query_order_events(account_id, since)
  - modules/execution/registry_offers.py:_AxiomQueryProtocol — fetch_events(**kwargs)

Single concrete class satisfies both via duck typing. Separate from
modules/admin/axiom_query.py:AxiomSmokeQueryAdapter (smoke verifies events
exist with 3-column projection; replay needs full payload reconstruction).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

_REPLAY_EVENT_TYPES = ("reservation_claimed", "order_fill", "reservation_released")


class AxiomReplayQueryAdapter:
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
        """Ledger replay protocol — events for one account from time anchor."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        apl = self._build_ledger_apl(
            dataset=self._dataset, account_id=account_id, since=since,
        )
        return await self._query_apl(apl)

    async def fetch_events(
        self,
        *,
        event_types: list[str],
        up_to_ms: int | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Registry replay protocol — events filtered by type and upper bound."""
        apl = self._build_registry_apl(
            dataset=self._dataset,
            event_types=event_types,
            up_to_ms=up_to_ms,
            account_id=account_id,
        )
        return await self._query_apl(apl)

    async def _query_apl(self, apl: str) -> list[dict[str, Any]]:
        resp = await self._http.post(
            "/v1/datasets/_apl?format=tabular",
            json={"apl": apl},
        )
        resp.raise_for_status()
        return self._tabular_to_rows(resp.json())

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _build_ledger_apl(
        *, dataset: str, account_id: str, since: datetime,
    ) -> str:
        # No `project` clause: Axiom flattens nested `payload` dict into
        # dot-notation columns (`payload.cid`, etc) on emit (4.4 prework
        # commit 9261f3f). `['payload']` is NOT a real column → projecting it
        # returns HTTP 400. Returning all columns lets `_tabular_to_rows`
        # re-nest the dot-notation columns into a `payload` dict.
        if "'" in account_id:
            raise ValueError(f"account_id contains illegal quote: {account_id!r}")
        types_list = ", ".join(f"'{t}'" for t in _REPLAY_EVENT_TYPES)
        return (
            f"['{dataset}']"
            f"\n| where ['event_type'] in ({types_list})"
            f"\n  and ['account_id'] == '{account_id}'"
            f"\n  and _time > datetime({since.isoformat()})"
            f"\n| order by _time asc"
        )

    @staticmethod
    def _build_registry_apl(
        *,
        dataset: str,
        event_types: list[str],
        up_to_ms: int | None,
        account_id: str | None,
    ) -> str:
        # See _build_ledger_apl note: no `project` clause due to Axiom payload
        # flattening (commit 9261f3f).
        if account_id is not None and "'" in account_id:
            raise ValueError(f"account_id contains illegal quote: {account_id!r}")
        for t in event_types:
            if "'" in t:
                raise ValueError(f"event_type contains illegal quote: {t!r}")
        types_list = ", ".join(f"'{t}'" for t in event_types)
        lines = [
            f"['{dataset}']",
            f"| where ['event_type'] in ({types_list})",
        ]
        if account_id is not None:
            lines.append(f"  and ['account_id'] == '{account_id}'")
        if up_to_ms is not None:
            iso = datetime.fromtimestamp(up_to_ms / 1000, tz=UTC).isoformat()
            lines.append(f"  and _time <= datetime({iso})")
        lines.append("| order by _time asc")
        return "\n".join(lines)

    @staticmethod
    def _tabular_to_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert Axiom tabular response to row dicts.

        Re-nests dot-notation payload columns (e.g. `payload.cid`) into a
        single `payload` dict, since Axiom flattens nested objects on emit
        (4.4 prework commit 9261f3f).
        """
        tables = payload.get("tables") or []
        if not tables:
            return []
        t = tables[0]
        fields = [f["name"] for f in t.get("fields", [])]
        cols = t.get("columns") or []
        if not fields or not cols:
            return []
        n_rows = len(cols[0])
        rows: list[dict[str, Any]] = []
        for ri in range(n_rows):
            row: dict[str, Any] = {}
            payload_nested: dict[str, Any] = {}
            payload_object_value: Any = None
            payload_object_seen = False
            for ci, field in enumerate(fields):
                value = cols[ci][ri]
                if field == "payload":
                    payload_object_value = value
                    payload_object_seen = True
                elif field.startswith("payload."):
                    key = field[len("payload."):]
                    payload_nested[key] = value
                else:
                    row[field] = value
            if payload_object_seen:
                # If Axiom returned `payload` as nested obj column, prefer it
                # but layer dot-notation keys on top (defensive merge).
                if isinstance(payload_object_value, dict):
                    row["payload"] = {**payload_object_value, **payload_nested}
                else:
                    row["payload"] = payload_nested or payload_object_value
            elif payload_nested:
                row["payload"] = payload_nested
            rows.append(row)
        return rows
