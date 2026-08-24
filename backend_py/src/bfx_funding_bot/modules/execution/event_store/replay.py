"""Authority object for decoding pre-versioned rows from the durable event log."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import inspect as sa_inspect

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow


class HistoricalReplayProvenance:
    """Proof that legacy decoding is tied to one persistent ``event_log`` row.

    The ordinary payload deserializer never accepts this authority and never
    upcasts unversioned dictionaries.  Only the stored-row decoder can create
    provenance, after SQLAlchemy confirms that the exact ORM row is persistent.
    """

    __slots__ = ("_consumed", "_payload_digest", "event_seq", "event_type")

    def __init__(
        self,
        *,
        event_seq: int,
        event_type: str,
        payload_digest: str,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _PROVENANCE_CONSTRUCTION_TOKEN:
            raise TypeError("historical replay provenance requires a persistent event_log row")
        self.event_seq = event_seq
        self.event_type = event_type
        self._payload_digest = payload_digest
        self._consumed = False

    @classmethod
    def from_stored_event(cls, row: EventLogRow) -> HistoricalReplayProvenance:
        state = sa_inspect(row)
        if not state.persistent or row.event_seq is None:
            raise TypeError("historical replay provenance requires a persistent event_log row")
        if not isinstance(row.payload, dict):
            raise TypeError("stored event payload must be an object")
        return cls(
            event_seq=row.event_seq,
            event_type=row.event_type,
            payload_digest=_payload_digest(row.payload),
            _construction_token=_PROVENANCE_CONSTRUCTION_TOKEN,
        )

    def authorizes(self, *, event_type: str, payload: dict[str, object]) -> bool:
        return self.event_type == event_type and self._payload_digest == _payload_digest(payload)

    def authorize_legacy_payload(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> _HistoricalReplayAuthorization:
        """Issue the one-shot internal authority for this exact stored payload."""
        if self._consumed:
            raise TypeError("historical replay provenance is already consumed")
        if not self.authorizes(event_type=event_type, payload=payload):
            raise TypeError("historical replay provenance does not match stored payload")
        self._consumed = True
        return _HistoricalReplayAuthorization(
            event_type=event_type,
            _construction_token=_AUTHORIZATION_CONSTRUCTION_TOKEN,
        )


class _HistoricalReplayAuthorization:
    """Private, single-use authority passed only to the legacy event factory."""

    __slots__ = ("_consumed", "_event_type")

    def __init__(self, *, event_type: str, _construction_token: object) -> None:
        if _construction_token is not _AUTHORIZATION_CONSTRUCTION_TOKEN:
            raise TypeError("historical replay authorization is internal")
        self._event_type = event_type
        self._consumed = False

    def consume(self, *, event_type: str) -> None:
        if self._consumed:
            raise TypeError("historical replay authorization is already consumed")
        if self._event_type != event_type:
            raise TypeError("historical replay authorization event type conflicts")
        self._consumed = True


def _payload_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


_PROVENANCE_CONSTRUCTION_TOKEN = object()
_AUTHORIZATION_CONSTRUCTION_TOKEN = object()
