"""Canonical immutable event-chain evidence shared by preflight and replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from bfx_funding_bot.modules.execution.event_store.serialization import (
    stored_event_identity,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow


def canonical_event_record(row: EventLogRow) -> dict[str, Any]:
    """Return the complete stable identity/content tuple for one stored event.

    ``recorded_at`` is intentionally excluded: it is storage metadata, not event
    content.  Historical rows use the same deterministic identity that replay
    uses, so a v2 row is not hashed differently merely because it lacks a native
    UUID column value.
    """
    if row.event_seq is None:
        raise ValueError("event stream has missing event sequence")
    identity = stored_event_identity(row)
    return {
        "event_seq": row.event_seq,
        "event_id": str(identity.event_id),
        "schema_version": row.schema_version,
        "event_type": row.event_type,
        "cid": row.cid,
        "venue_offer_id": row.venue_offer_id,
        "venue_seq": row.venue_seq,
        "payload": row.payload,
        "occurred_at_ms": row.occurred_at_ms,
    }


def canonical_event_hash(rows: Sequence[EventLogRow]) -> str:
    """Hash an ordered event stream with the one preflight/replay contract."""
    encoded = json.dumps(
        [canonical_event_record(row) for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["canonical_event_hash", "canonical_event_record"]
