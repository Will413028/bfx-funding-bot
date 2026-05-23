"""Unit tests for AxiomReplayQueryAdapter pure functions + I/O shell."""
from __future__ import annotations

from datetime import UTC, datetime

from bfx_funding_bot.modules.execution.axiom_event_query import (
    AxiomReplayQueryAdapter,
)


def test_build_ledger_apl_includes_event_types_and_account_and_since() -> None:
    apl = AxiomReplayQueryAdapter._build_ledger_apl(
        dataset="bfx-events",
        account_id="default",
        since=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
    )
    assert "['bfx-events']" in apl
    assert "['account_id'] == 'default'" in apl
    assert "datetime(2026-05-01T12:00:00+00:00)" in apl
    assert "reservation_claimed" in apl
    assert "order_fill" in apl
    assert "reservation_released" in apl
    assert "order by _time asc" in apl
    # Project all fields
    assert "['payload']" in apl
    assert "['correlation_id']" in apl


def test_build_registry_apl_no_account_filter_when_none() -> None:
    apl = AxiomReplayQueryAdapter._build_registry_apl(
        dataset="bfx-events",
        event_types=["reservation_claimed", "order_fill"],
        up_to_ms=None,
        account_id=None,
    )
    assert "['bfx-events']" in apl
    assert "['account_id']" not in apl  # no filter
    assert "reservation_claimed" in apl
    assert "order_fill" in apl
    assert "_time <=" not in apl  # no upper bound


def test_build_registry_apl_with_up_to_ms_and_account() -> None:
    apl = AxiomReplayQueryAdapter._build_registry_apl(
        dataset="bfx-events",
        event_types=["reservation_claimed"],
        up_to_ms=1700000000000,  # 2023-11-14T22:13:20Z
        account_id="acct-1",
    )
    assert "['account_id'] == 'acct-1'" in apl
    assert "_time <= datetime(2023-11-14T22:13:20+00:00)" in apl


def test_build_apl_escapes_quote_in_account_id_via_replacement() -> None:
    # Defensive: APL is single-quoted; embedded single-quote must not break query.
    # Implementation choice: reject account_id containing "'" (raise ValueError).
    import pytest
    with pytest.raises(ValueError, match="account_id"):
        AxiomReplayQueryAdapter._build_ledger_apl(
            dataset="bfx-events",
            account_id="bad'id",
            since=datetime(2026, 5, 1, tzinfo=UTC),
        )
