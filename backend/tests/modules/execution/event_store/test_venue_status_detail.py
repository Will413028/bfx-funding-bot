"""An offer that filled must be recognised as finished.

Bitfinex reports offer state with narrative appended -- "EXECUTED at 0.0148%
(150.78)" is what it returned for the canary filled on 2026-09-23. Normalising
by lower-casing and replacing spaces turned that into the unknown status
"executed_at_0.0148%_(150.78)", so the capital classifier could never read the
offer as terminal and refused to account for the money.
"""
import pytest

from bfx_funding_bot.modules.execution.event_store.entities import (
    is_terminal_offer_status,
    normalize_venue_status,
)
from bfx_funding_bot.modules.execution.event_store.projector import (
    _normalize_status as projector_normalize,
)


@pytest.mark.parametrize("raw,state", [
    ("EXECUTED at 0.0148% (150.78)", "executed"),       # verbatim, 2026-09-23
    ("EXECUTED at 0.0178% (392.3)", "executed"),
    ("PARTIALLY FILLED at 0.02% (50.0)", "partially_filled"),
    ("CANCELED was: PARTIALLY FILLED at 0.02% (50.0)", "canceled"),
    ("EXECUTED @ 0.0148%(150.78)", "executed"),
    ("CANCELED", "canceled"),
    ("ACTIVE", "active"),
    ("  Active  ", "active"),
])
def test_the_state_is_the_leading_phrase(raw: str, state: str) -> None:
    assert normalize_venue_status(raw) == state


def test_a_filled_offer_reads_as_terminal() -> None:
    assert is_terminal_offer_status("EXECUTED at 0.0148% (150.78)")


def test_a_partial_fill_does_not() -> None:
    assert not is_terminal_offer_status("PARTIALLY FILLED at 0.02% (50.0)")


def test_the_projector_and_the_entities_agree() -> None:
    """Two copies of this rule disagreed once; there is now one."""
    for raw in ("EXECUTED at 0.0148% (150.78)", "CANCELED was: PARTIALLY FILLED at x", "ACTIVE"):
        assert projector_normalize(raw) == normalize_venue_status(raw)
