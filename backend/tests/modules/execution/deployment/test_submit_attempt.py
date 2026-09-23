"""SubmitAttemptRecorder — what the money path last actually tried to do.

Motivation (2026-07-27 incident): asked "is the bot placing orders right now?"
the only available answers were log greps and env vars. `deployment_skip
guard=... reason=...` was already being logged at exactly the right place — it
just went nowhere queryable. This records the same three outcomes into a slot
the status endpoint can read.

`started_at` matters as much as the attempt: `last is None` means "this process
has not tried since <start>", NOT "the bot has never traded".
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)


def _clock(*stamps: datetime) -> object:
    it = iter(stamps)
    return lambda: next(it)


def test_starts_empty_but_records_when_the_process_started() -> None:
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    r = SubmitAttemptRecorder(clock=_clock(start))
    assert r.last is None
    assert r.started_at == start


def test_records_a_guard_block_with_the_blocking_guard_and_reason() -> None:
    start = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    at = datetime(2026, 7, 27, 10, 5, tzinfo=UTC)
    r = SubmitAttemptRecorder(clock=_clock(start, at))
    r.record_blocked(
        cell="c1", symbol="fUST", amount=Decimal("150"),
        guard_name="manual_kill", reason="BFX_KILL_SWITCH env flag set",
    )
    assert r.last is not None
    assert r.last.outcome == "blocked"
    assert r.last.at == at
    assert r.last.cell == "c1"
    assert r.last.symbol == "fUST"
    assert r.last.amount == Decimal("150")
    assert r.last.guard_name == "manual_kill"
    assert r.last.reason == "BFX_KILL_SWITCH env flag set"


def test_records_a_successful_submit() -> None:
    r = SubmitAttemptRecorder(
        clock=_clock(datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
                     datetime(2026, 7, 27, 10, 5, tzinfo=UTC)),
    )
    r.record_submitted(cell="c1", symbol="fUST", amount=Decimal("150"))
    assert r.last is not None
    assert r.last.outcome == "submitted"
    assert r.last.guard_name is None


def test_records_a_venue_rejection_distinctly_from_a_guard_block() -> None:
    """A venue reject means the guards PASSED and the offer left the process —
    collapsing it into "blocked" would read as a working halt."""
    r = SubmitAttemptRecorder(
        clock=_clock(datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
                     datetime(2026, 7, 27, 10, 5, tzinfo=UTC)),
    )
    r.record_rejected(
        cell="c1", symbol="fUST", amount=Decimal("150"), reason="10001 balance",
    )
    assert r.last is not None
    assert r.last.outcome == "rejected"
    assert r.last.reason == "10001 balance"


def test_records_a_submit_exception() -> None:
    r = SubmitAttemptRecorder(
        clock=_clock(datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
                     datetime(2026, 7, 27, 10, 5, tzinfo=UTC)),
    )
    r.record_error(cell="c1", symbol="fUST", amount=Decimal("150"), reason="TimeoutError")
    assert r.last is not None
    assert r.last.outcome == "error"


def test_latest_attempt_replaces_the_previous_one() -> None:
    r = SubmitAttemptRecorder(
        clock=_clock(
            datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 10, 5, tzinfo=UTC),
            datetime(2026, 7, 27, 10, 6, tzinfo=UTC),
        ),
    )
    r.record_blocked(
        cell="c1", symbol="fUST", amount=Decimal("150"),
        guard_name="manual_kill", reason="halted",
    )
    r.record_submitted(cell="c2", symbol="fUST", amount=Decimal("200"))
    assert r.last is not None
    assert r.last.outcome == "submitted"
    assert r.last.cell == "c2"


def test_as_dict_is_json_safe_and_keeps_amount_exact() -> None:
    """Served over HTTP: Decimal and datetime must already be primitives, and
    the amount must not go through float (real-money figure in a report an
    operator uses to decide whether to intervene)."""
    r = SubmitAttemptRecorder(
        clock=_clock(datetime(2026, 7, 27, 10, 0, tzinfo=UTC),
                     datetime(2026, 7, 27, 10, 5, tzinfo=UTC)),
    )
    r.record_blocked(
        cell="c1", symbol="fUST", amount=Decimal("150.25"),
        guard_name="manual_kill", reason="halted",
    )
    d = r.as_dict()
    assert d == {
        "at": "2026-07-27T10:05:00+00:00",
        "cell": "c1",
        "symbol": "fUST",
        "amount": "150.25",
        "outcome": "blocked",
        "guard_name": "manual_kill",
        "reason": "halted",
    }


def test_as_dict_is_none_before_any_attempt() -> None:
    r = SubmitAttemptRecorder(clock=_clock(datetime(2026, 7, 27, 10, 0, tzinfo=UTC)))
    assert r.as_dict() is None


def test_default_clock_produces_timezone_aware_utc() -> None:
    r = SubmitAttemptRecorder()
    r.record_submitted(cell="c1", symbol="fUST", amount=Decimal("1"))
    assert r.started_at.tzinfo is not None
    assert r.last is not None
    assert r.last.at.tzinfo is not None
