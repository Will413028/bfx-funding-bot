"""EventRecorder — spy/recorder pattern for smoke verification."""
from __future__ import annotations

from dataclasses import dataclass

from bfx_funding_bot.modules.admin.smoke_runner import EventRecorder


@dataclass
class _FakeEvent:
    account_id: str
    payload: str


async def test_recorder_captures_matching_account_id() -> None:
    recorder = EventRecorder(account_id="smoke_test")
    e = _FakeEvent(account_id="smoke_test", payload="x")
    await recorder.record(e)
    assert recorder.events == [e]


async def test_recorder_skips_foreign_account_id() -> None:
    recorder = EventRecorder(account_id="smoke_test")
    await recorder.record(_FakeEvent(account_id="default", payload="x"))
    assert recorder.events == []


async def test_recorder_preserves_order() -> None:
    recorder = EventRecorder(account_id="smoke_test")
    events = [_FakeEvent(account_id="smoke_test", payload=str(i)) for i in range(5)]
    for e in events:
        await recorder.record(e)
    assert recorder.events == events


async def test_recorder_skips_event_without_account_id_attribute() -> None:
    """Defensive: a malformed event without account_id should be ignored, not crash."""
    recorder = EventRecorder(account_id="smoke_test")
    await recorder.record(object())  # no account_id attr
    assert recorder.events == []
