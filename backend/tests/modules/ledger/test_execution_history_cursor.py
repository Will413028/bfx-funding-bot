"""The execution history's journal cursor: ``at:rank:id``, ranks 0-2 (journal) and 3-4 (venue ends).

Mutation: parse ranks as ``("0", "1", "2")`` again: the fill and credit-end cases fail.
"""

from __future__ import annotations

import pytest

from bfx_funding_bot.modules.ledger import ExecutionCursorError
from bfx_funding_bot.modules.ledger._internal.execution_history import (
    _journal_position,
    _token,
    _untoken,
)


@pytest.mark.parametrize(
    ("body", "position"),
    [
        # Cursors issued before fills and credit ends existed stay valid.
        ("3000:0:5b0c4a8e-0000-0000-0000-000000000001",
         (3000, 0, "5b0c4a8e-0000-0000-0000-000000000001")),
        ("3200:2:7", (3200, 2, "7")),
        ("2500:3:f-1", (2500, 3, "f-1")),
        # A credit end's id is ``source_kind:venue_credit_id``: only the first two colons split.
        ("2700:4:credit:77", (2700, 4, "credit:77")),
    ],
)
def test_a_journal_cursor_round_trips(body: str, position: tuple[int, int, str]) -> None:
    token = _token("j", body)
    assert _untoken(token) == ("j", body)
    assert _journal_position(token, body) == position


@pytest.mark.parametrize("body", ["2500:5:f-1", "2500:-1:f-1", "2500:3:", "x:3:f-1", "2500"])
def test_an_unknown_rank_or_malformed_body_is_refused(body: str) -> None:
    with pytest.raises(ExecutionCursorError):
        _journal_position(_token("j", body), body)
