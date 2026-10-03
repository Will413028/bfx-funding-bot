"""The ledger's conservation verdict, and its equality with the legacy rule.

Mutations (apply one at a time in ``modules/ledger/conservation.py``, run this file, revert):

1. ``max(Decimal(0), -offered_change)`` -> ``-offered_change``:
   ``test_verdict_table`` (``offer_added_without_lending``), ``test_matches_legacy_on_a_grid``.
4. drop the epsilon (``<= epsilon`` -> ``<= 0``, ``> epsilon`` -> ``> 0``):
   ``test_verdict_table`` (``epsilon_*``), ``test_matches_legacy_on_a_grid``.
6. a missing previous row compared with zero instead of a baseline:
   ``test_verdict_table`` (``no_previous_row``).
9. a foreign-covered remainder classified ``unexplained_lending``:
   ``test_verdict_table`` (``foreign_covers``), ``test_matches_legacy_on_a_grid``.

Declared divergence from legacy (outside this pure layer, so no case here is weakened): legacy
``BootRecovery._foreign_executed`` counts every foreign offer that ended after the last accepted
query began, including one that was already in the prior ledger; the ledger counts only those
absent from the previous accepted observation, because a previous offer's remaining leaving
``offered`` already explains its later fills (``test_owner_state_presence_matrix`` in
``tests/integration/test_ledger_conservation.py``). The function and its inputs' meaning are
otherwise identical, which the grid below pins.

The acceptance-side mutations (2, 3, 5, 7, 8) are in ``tests/integration/test_ledger_conservation.py``
and ``tests/modules/execution/safety/test_protection.py``.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from bfx_funding_bot.modules import ledger
from bfx_funding_bot.modules.execution.event_store.store import SymbolLedgerDelta
from bfx_funding_bot.modules.execution.safety import protection
from bfx_funding_bot.modules.execution.safety.protection import LedgerConservation
from bfx_funding_bot.modules.ledger.conservation import (
    CONSERVATION_VERDICTS,
    LEDGER_EPSILON,
    ConservationVerdict,
    SymbolFlow,
    conservation_verdict,
)

D = Decimal


def _flow(offered: str, lent: str) -> SymbolFlow:
    return SymbolFlow(D(offered), D(lent))


# name -> (previous, current, foreign_executed, placed, expected verdict, lent_unexplained)
CASES = {
    "no_previous_row": (None, _flow("0", "500"), "0", "0", "baseline", "0"),
    "nothing_changed": (_flow("300", "200"), _flow("300", "200"), "0", "0", "conserved", "0"),
    "loan_ended": (_flow("0", "200"), _flow("0", "50"), "0", "0", "conserved", "0"),
    "fill_explained_by_offered_drop": (
        _flow("300", "0"), _flow("100", "200"), "0", "0", "conserved", "0",
    ),
    "fill_larger_than_offered_drop": (
        _flow("300", "0"), _flow("100", "500"), "0", "0", "unexplained_lending", "300",
    ),
    "offer_added_without_lending": (
        _flow("0", "0"), _flow("100", "0"), "0", "0", "conserved", "0",
    ),
    "offer_added_and_lent": (
        _flow("0", "0"), _flow("100", "100"), "0", "0", "unexplained_lending", "100",
    ),
    "placed_then_filled_between_bases": (
        _flow("0", "0"), _flow("50", "150"), "0", "200", "conserved", "0",
    ),
    "placed_covers_only_its_amount": (
        _flow("0", "0"), _flow("50", "400"), "0", "200", "unexplained_lending", "250",
    ),
    "foreign_covers": (_flow("0", "0"), _flow("0", "100"), "100", "0", "foreign_lending", "100"),
    "foreign_covers_all_but_epsilon": (
        _flow("0", "0"), _flow("0", "100"), "99.99", "0", "foreign_lending", "100",
    ),
    "foreign_partial_cover": (
        _flow("0", "0"), _flow("0", "100"), "99.98", "0", "unexplained_lending", "100",
    ),
    "foreign_without_lending_is_conserved": (
        _flow("0", "0"), _flow("0", "0"), "100", "0", "conserved", "0",
    ),
    "epsilon_edge_is_conserved": (
        _flow("0", "0"), _flow("0", "0.01"), "0", "0", "conserved", "0.01",
    ),
    "epsilon_plus_one_is_unexplained": (
        _flow("0", "0"), _flow("0", "0.02"), "0", "0", "unexplained_lending", "0.02",
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_verdict_table(name: str) -> None:
    previous, current, foreign, placed, verdict, unexplained = CASES[name]
    assert conservation_verdict(previous, current, D(foreign), D(placed)) == ConservationVerdict(
        verdict,  # type: ignore[arg-type]
        D(unexplained),
        D(0) if verdict == "baseline" else D(foreign),
    )


def test_the_epsilon_is_the_one_protection_uses() -> None:
    assert protection.LEDGER_EPSILON is LEDGER_EPSILON is ledger.LEDGER_EPSILON
    assert D("0.01") == LEDGER_EPSILON
    assert LedgerConservation()._epsilon == LEDGER_EPSILON


def _legacy(
    previous: SymbolFlow, current: SymbolFlow, foreign: Decimal, placed: Decimal
) -> str:
    """Legacy's single step: its ledger already held what this bot placed (no carry)."""
    verdict = LedgerConservation().observe(
        [
            SymbolLedgerDelta(
                "fUST", previous.offered + placed, previous.lent, current.offered, current.lent, True
            )
        ],
        confirmed=True,
        foreign_executed={"fUST": foreign},
    )
    if verdict.anomalies:
        return "unexplained_lending"
    return "foreign_lending" if verdict.foreign else "conserved"


_AMOUNTS = [D(x) for x in ("0", "0.01", "0.02", "5", "100", "250")]


def test_matches_legacy_on_a_grid() -> None:
    """Every combination of the boundary and ordinary amounts; one step, no carry."""
    checked = 0
    for p_off, p_lent, c_off, c_lent, foreign, placed in itertools.product(
        _AMOUNTS, _AMOUNTS, _AMOUNTS, _AMOUNTS, [*_AMOUNTS, D("99.99")], [D(0), D("100")]
    ):
        previous, current = SymbolFlow(p_off, p_lent), SymbolFlow(c_off, c_lent)
        ours = conservation_verdict(previous, current, foreign, placed).conservation
        assert ours == _legacy(previous, current, foreign, placed), (
            previous, current, foreign, placed,
        )
        checked += 1
    assert checked > 10_000


def test_every_stored_verdict_is_named() -> None:
    assert set(CONSERVATION_VERDICTS) == {
        "baseline", "conserved", "foreign_lending", "unexplained_lending",
    }
