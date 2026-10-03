"""The ledger's id-level conservation verdict (pure layer).

This replaces the legacy ``LedgerConservation`` offered-drop heuristic, which cannot tell a
cancel from a fill, so there is no differential test against it: the two are a documented
divergence (``ledger/conservation.py``), not a pair to keep equal case by case. The property test
below is what pins the rule instead.

Mutations (apply one at a time in ``modules/ledger/conservation.py``, run this file, revert):

a. count credit decreases as lending (``max(0, c - p)`` -> ``c - p``):
   ``test_verdict_table`` (``loan_end_plus_unexplained_credit``), ``test_property_*``.
b. treat a negative unexplained as conserved (``abs(unexplained)`` -> ``unexplained``):
   ``test_verdict_table`` (``fill_without_credit``), ``test_property_*``.
c. classify a foreign fill as own (``fill.foreign`` ignored): ``test_verdict_table``
   (``foreign_fill_is_an_alert``).
d. ignore a conflict (``conflicts`` dropped from the condition):
   ``test_verdict_table`` (``conflict_*``).
The id-level mutations (initial amount vs previous remaining, dropping terminal-history fills,
ignoring the trades/remaining disagreement) are in ``tests/integration/test_ledger_conservation.py``.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from bfx_funding_bot.modules import ledger
from bfx_funding_bot.modules.execution.safety import protection
from bfx_funding_bot.modules.ledger.conservation import (
    CONSERVATION_VERDICTS,
    LEDGER_EPSILON,
    ConservationVerdict,
    OfferFill,
    conservation_verdict,
    new_lending,
)

D = Decimal


def own(amount: str, *, conflict: bool = False) -> OfferFill:
    return OfferFill(D(amount), False, conflict)


def foreign(amount: str) -> OfferFill:
    return OfferFill(D(amount), True)


def credits(**amounts: str) -> dict[str, D]:
    return {key: D(value) for key, value in amounts.items()}


# name -> (previous, current, fills, verdict, lent_unexplained, foreign_executed, conflicts)
CASES = {
    "no_previous_row": (None, credits(a="500"), [], "baseline", "0", "0", 0),
    "nothing_changed": (credits(a="200"), credits(a="200"), [], "conserved", "0", "0", 0),
    "loan_ended": (credits(a="200"), credits(a="50"), [], "conserved", "0", "0", 0),
    "every_loan_ended": (credits(a="200"), {}, [], "conserved", "0", "0", 0),
    "own_fill_explains_new_credit": (
        credits(a="10"),
        credits(a="10", b="60"),
        [own("60")],
        "conserved",
        "0",
        "0",
        0,
    ),
    "foreign_fill_is_an_alert": (
        credits(),
        credits(b="60"),
        [foreign("60")],
        "foreign_lending",
        "0",
        "60",
        0,
    ),
    "cancel_is_not_lending": (
        credits(a="10"),
        credits(a="10"),
        [own("0")],
        "conserved",
        "0",
        "0",
        0,
    ),
    "unexplained_credit": (credits(), credits(b="60"), [], "unexplained_lending", "60", "0", 0),
    "partial_explanation": (
        credits(),
        credits(b="100"),
        [own("40")],
        "unexplained_lending",
        "60",
        "0",
        0,
    ),
    "fill_without_credit": (
        credits(),
        credits(),
        [own("60")],
        "unexplained_lending",
        "-60",
        "0",
        0,
    ),
    "loan_end_plus_unexplained_credit": (
        credits(a="100"),
        credits(b="70"),
        [],
        "unexplained_lending",
        "70",
        "0",
        0,
    ),
    "cancel_and_loan_end_plus_unexplained_credit": (
        credits(a="100"),
        credits(b="70"),
        [own("0")],
        "unexplained_lending",
        "70",
        "0",
        0,
    ),
    "loan_end_does_not_offset_a_fill": (
        credits(a="100"),
        credits(a="40", b="60"),
        [own("30")],
        "unexplained_lending",
        "30",
        "0",
        0,
    ),
    "split_under_different_keys_looks_new": (
        credits(a="100"),
        credits(a="40", b="60"),
        [],
        "unexplained_lending",
        "60",
        "0",
        0,
    ),
    "same_key_split_is_not_new": (credits(k="100"), credits(k="100"), [], "conserved", "0", "0", 0),
    "conflict_alone": (
        credits(),
        credits(),
        [own("0", conflict=True)],
        "unexplained_lending",
        "0",
        "0",
        1,
    ),
    "conflict_even_if_sums_agree": (
        credits(),
        credits(b="60"),
        [own("60"), own("0", conflict=True)],
        "unexplained_lending",
        "0",
        "0",
        1,
    ),
    "negative_fill_is_a_conflict": (
        credits(),
        credits(),
        [own("-5")],
        "unexplained_lending",
        "0",
        "0",
        1,
    ),
    "epsilon_edge_is_conserved": (credits(), credits(b="0.01"), [], "conserved", "0.01", "0", 0),
    "epsilon_plus_one_is_unexplained": (
        credits(),
        credits(b="0.02"),
        [],
        "unexplained_lending",
        "0.02",
        "0",
        0,
    ),
    "negative_epsilon_edge_is_conserved": (
        credits(),
        credits(),
        [own("0.01")],
        "conserved",
        "-0.01",
        "0",
        0,
    ),
    "negative_epsilon_plus_one_is_unexplained": (
        credits(),
        credits(),
        [own("0.02")],
        "unexplained_lending",
        "-0.02",
        "0",
        0,
    ),
    "foreign_dust_is_not_an_alert": (
        credits(),
        credits(b="0.01"),
        [foreign("0.01")],
        "conserved",
        "0",
        "0.01",
        0,
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_verdict_table(name: str) -> None:
    previous, current, fills, verdict, unexplained, foreign_executed, conflicts = CASES[name]
    found = conservation_verdict(previous, current, fills)
    assert found == ConservationVerdict(
        verdict,  # type: ignore[arg-type]
        D(unexplained),
        D(foreign_executed),
        conflicts,
    ), found


def test_new_lending_counts_increases_only() -> None:
    assert new_lending(credits(a="100", b="5"), credits(a="30", b="9", c="2")) == D("6")


def test_the_epsilon_is_the_one_protection_uses() -> None:
    assert protection.LEDGER_EPSILON is LEDGER_EPSILON is ledger.LEDGER_EPSILON
    assert D("0.01") == LEDGER_EPSILON


def test_every_stored_verdict_is_named() -> None:
    assert set(CONSERVATION_VERDICTS) == {
        "baseline",
        "conserved",
        "foreign_lending",
        "unexplained_lending",
    }


# --- property: any sequence of id-level movements --------------------------------------------

_MOVES = ("fill_own", "fill_foreign", "cancel", "loan_end", "loan_shrink", "recut")


def _world(rng: random.Random) -> dict[str, D]:
    return {f"p{i}": D(rng.randint(10, 500)) for i in range(rng.randint(0, 4))}


def _interval(
    rng: random.Random, before: dict[str, D]
) -> tuple[dict[str, D], list[OfferFill], D]:
    """Apply random movements; return the credits after, the fills and the foreign part of them."""
    after = dict(before)
    fills: list[OfferFill] = []
    foreign_part = D(0)
    for number, move in enumerate(rng.choices(_MOVES, k=rng.randint(0, 8))):
        if move in ("fill_own", "fill_foreign"):
            amount = D(rng.randint(1, 300))
            is_foreign = move == "fill_foreign"
            fills.append(OfferFill(amount, is_foreign))
            after[f"n{number}"] = amount
            foreign_part += amount if is_foreign else 0
        elif move == "cancel":
            fills.append(OfferFill(D(0), rng.random() < 0.5))
        elif move == "loan_end" and after.keys() & before.keys():
            after.pop(rng.choice(sorted(after.keys() & before.keys())))
        elif move == "loan_shrink" and after.keys() & before.keys():
            key = rng.choice(sorted(after.keys() & before.keys()))
            after[key] = max(D(0), after[key] - D(rng.randint(1, 50)))
        elif move == "recut":
            # The venue replaces one lending's ids by others of the same total: same key.
            pass
    return after, fills, foreign_part


@pytest.mark.parametrize("seed", range(300))
def test_property_conserved_iff_lending_equals_fills(seed: int) -> None:
    rng = random.Random(seed)
    before = _world(rng)
    after, fills, foreign_part = _interval(rng, before)
    verdict = conservation_verdict(before, after, fills)
    assert verdict.conservation in ("conserved", "foreign_lending"), (seed, verdict)
    assert verdict.conservation == (
        "foreign_lending" if foreign_part > LEDGER_EPSILON else "conserved"
    )
    assert verdict.lent_unexplained == 0 and verdict.fill_conflicts == 0
    # Equality of lending and fills is what makes it conserved: perturb either side.
    injected = D(rng.choice(["0.05", "1", "40"]))
    extra = dict(after)
    extra["injected"] = injected
    assert conservation_verdict(before, extra, fills).conservation == "unexplained_lending"
    assert (
        conservation_verdict(before, after, [*fills, OfferFill(injected, False)]).lent_unexplained
        == -injected
    )


@pytest.mark.parametrize("seed", range(300))
def test_property_an_injected_credit_is_caught_beside_cancels_and_loan_ends(seed: int) -> None:
    rng = random.Random(10_000 + seed)
    before = _world(rng) or {"p0": D(100)}
    after, fills, _ = _interval(rng, before)
    # A cancel and a loan end in the same interval, then an unexplained credit.
    fills.append(OfferFill(D(0), False))
    after.pop(sorted(before)[0], None)
    injected = D(rng.choice(["0.05", "3", "250"]))
    after["injected"] = after.get("injected", D(0)) + injected
    verdict = conservation_verdict(before, after, fills)
    assert verdict.conservation == "unexplained_lending", (seed, verdict)
    assert verdict.lent_unexplained == injected
