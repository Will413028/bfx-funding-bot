"""``c3f5a1d7e204`` seals the existing events with the writer's prefix chain.

The migration carries the writer's record contract as its own copy (it must not import the
event store, whose ORM moved to ``legacy_archive`` in ``c2d3e4f5a6b7``). This pins that copy
to the live contract (``event_store.canonical.rolling_prefix_hashes``) on unversioned v2,
versioned v2 and native v3 rows across two scopes. It goes with the event store's remainder.

Mutation: change the copy's v2 namespace or the record's key set: the chains differ.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

from bfx_funding_bot.modules.execution.event_store.canonical import rolling_prefix_hashes
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from tests.pg_templates import alembic

pytestmark = pytest.mark.integration

_PARENT = "b4e6f8a0c203"
_REVISION = "c3f5a1d7e204"
_A = "00000000-0000-0000-0000-0000000c3f51"
_B = "00000000-0000-0000-0000-0000000c3f52"
_V3 = "4f6d1c2a-9b8e-4c3d-a1f2-0e9d8c7b6a51"
# (account, environment, event_type, schema_version, payload, event_id)
_ROWS: tuple[tuple[str, str, str, int, dict[str, Any], str | None], ...] = (
    (_A, "ci", "RESERVATION_INTENT", 2, {"symbol": "fUST", "size_usdt": "150"}, None),
    (_B, "ci", "ORDER_FILL", 2, {"__schema_version__": 2, "__event_type__": "ORDER_FILL",
                                 "amount": "10", "fill_rate": 0.0002}, None),
    (_A, "ci", "ORDER_FILL", 3, {"__schema_version__": 3, "__event_type__": "ORDER_FILL",
                                 "event_id": _V3, "amount": "150"}, _V3),
    (_A, "shadow", "RESERVATION_RELEASED", 2, {"reason": "expired"}, None),
    (_B, "ci", "RESERVATION_INTENT", 2, {"symbol": "fUSD", "size_usdt": "7"}, None),
)


def _build_parent(url: str) -> None:
    alembic(url, "upgrade", _PARENT)


def test_the_migration_seals_events_with_the_writers_chain(pg_templates, pg_clone) -> None:
    url = pg_clone(pg_templates.template(f"event_prefix_parent_{_PARENT}", _build_parent))
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for account in (_A, _B):
                conn.execute(text("INSERT INTO exchange_accounts (id, venue, label) "
                                  "VALUES (:id, 'bitfinex', 'c3f5')"), {"id": account})
            for seq, (account, env, event_type, version, payload, event_id) in enumerate(
                    _ROWS, start=1):
                conn.execute(text(
                    "INSERT INTO event_log (account_id, exchange_account_id, "
                    "deployment_environment, event_type, cid, venue_offer_id, venue_seq, "
                    "event_id, schema_version, payload, occurred_at_ms) VALUES (:account, "
                    ":account_uuid, :env, :type, :cid, :offer, :venue_seq, :event_id, :version, "
                    "CAST(:payload AS jsonb), :at)"),
                    {"account": account, "account_uuid": UUID(account), "env": env, "type": event_type, "cid": seq,
                     "offer": f"offer-{seq}" if seq % 2 else None,
                     "venue_seq": seq * 10 if seq % 2 else None,
                     "event_id": UUID(event_id) if event_id else None,
                     "version": version, "payload": json.dumps(payload), "at": 1000 + seq})
        alembic(url, "upgrade", _REVISION)
        with engine.connect() as conn:
            rows = [dict(row._mapping) for row in conn.execute(text(
                "SELECT event_seq, account_id, exchange_account_id, deployment_environment, "
                "event_type, cid, venue_offer_id, venue_seq, event_id, schema_version, payload, "
                "occurred_at_ms FROM event_log ORDER BY event_seq"))]
            sealed = {row.event_seq: row.prefix_hash for row in conn.execute(text(
                "SELECT event_seq, prefix_hash FROM event_prefix_hashes"))}
    finally:
        engine.dispose()
    expected: dict[int, str] = {}
    for scope in sorted({(str(r["exchange_account_id"]), r["deployment_environment"])
                         for r in rows}):
        stream = [EventLogRow(**r) for r in rows
                  if (str(r["exchange_account_id"]), r["deployment_environment"]) == scope]
        expected |= dict(zip((r.event_seq for r in stream), rolling_prefix_hashes(stream),
                             strict=True))
    assert len(rows) == len(_ROWS)
    assert sealed == expected
    assert isinstance(rows[2]["event_id"], UUID)
