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
from typing import Any  # noqa: F401

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

    @staticmethod
    def _build_ledger_apl(
        *, dataset: str, account_id: str, since: datetime,
    ) -> str:
        if "'" in account_id:
            raise ValueError(f"account_id contains illegal quote: {account_id!r}")
        types_list = ", ".join(f"'{t}'" for t in _REPLAY_EVENT_TYPES)
        return (
            f"['{dataset}']"
            f"\n| where ['event_type'] in ({types_list})"
            f"\n  and ['account_id'] == '{account_id}'"
            f"\n  and _time > datetime({since.isoformat()})"
            f"\n| project _time, ['event_type'], ['account_id'],"
            f" ['correlation_id'], ['payload']"
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
        lines.extend([
            "| project _time, ['event_type'], ['correlation_id'], ['payload']",
            "| order by _time asc",
        ])
        return "\n".join(lines)
