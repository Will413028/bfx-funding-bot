"""D3a amount fingerprints (the live codec in deployment/fingerprinted_amount.py): bounds,
determinism and collision probing."""
from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.modules.execution.deployment.fingerprinted_amount import (
    FINGERPRINT_SPACE,
    choose_fingerprinted_amount,
    fingerprint_seed,
    fingerprinted,
)
from bfx_funding_bot.modules.trading import fingerprint_of

D = Decimal


def test_fingerprint_is_the_last_four_of_eight_decimals():
    assert fingerprint_of(D("123.45670042")) == 42
    assert fingerprint_of(D("200")) == 0
    assert fingerprint_of(D("0.00009999")) == 9999
    # Not a wire amount: finer than the venue quantum, non-positive or non-finite.
    for invalid in (D("1.000000001"), D("0"), D("-1.00000001"), D("NaN"), "x"):
        assert fingerprint_of(invalid) is None


@pytest.mark.parametrize(("planned", "fingerprint", "expected"), [
    # Rounded down to 4 decimals, then the fingerprint added when that fits.
    (D("700.12345678"), 42, D("700.12340042")),
    (D("700.12349999"), 9999, D("700.12349999")),
    # Adding it would exceed the plan: one 0.0001 step down first.
    (D("700"), 42, D("699.99990042")),
    (D("700.12340041"), 42, D("699.99990042") + D("0.1234")),
])
def test_fingerprinted_amount_never_exceeds_the_plan(planned, fingerprint, expected):
    amount = fingerprinted(planned, fingerprint)
    assert amount == expected
    assert amount <= planned
    assert planned - amount < D("0.0001")
    assert amount == amount.quantize(D("0.00000001"))
    assert fingerprint_of(amount) == fingerprint


def test_choice_is_deterministic_per_identity_and_spread_across_identities():
    kwargs = {"in_use": frozenset(), "minimum": D("150"), "maximum": D("1000")}
    first = choose_fingerprinted_amount(D("500"), seed_key="reconcile:1:a30:s", **kwargs)
    again = choose_fingerprinted_amount(D("500"), seed_key="reconcile:1:a30:s", **kwargs)
    assert first == again
    assert first is not None and fingerprint_of(first) == fingerprint_seed("reconcile:1:a30:s")
    others = {fingerprint_of(choose_fingerprinted_amount(D("500"), seed_key=f"k{i}", **kwargs))
              for i in range(50)}
    assert len(others) > 40  # a hash, not a constant


def test_choice_probes_forward_past_fingerprints_in_use():
    key = "reconcile:7:p2:signal"
    seed = fingerprint_seed(key)
    taken = {seed, seed % FINGERPRINT_SPACE + 1}
    amount = choose_fingerprinted_amount(D("500"), seed_key=key, in_use=taken,
                                         minimum=D("150"), maximum=None)
    assert amount is not None
    assert fingerprint_of(amount) == (seed + 1) % FINGERPRINT_SPACE + 1
    assert fingerprint_of(amount) not in taken


def test_choice_respects_minimum_and_maximum_and_fails_closed():
    # Planned exactly at the minimum: only fingerprints that need no step down
    # fit, and a whole-number plan has none -- skip rather than go below it.
    assert choose_fingerprinted_amount(D("150"), seed_key="k", in_use=frozenset(),
                                       minimum=D("150"), maximum=None) is None
    # With room for exactly the residual, the one fitting fingerprint is found.
    amount = choose_fingerprinted_amount(D("150.00000007"), seed_key="k", in_use=frozenset(),
                                         minimum=D("150"), maximum=D("150.00000007"))
    assert amount is not None and D("150") <= amount <= D("150.00000007")
    # Every fingerprint taken: no submit.
    assert choose_fingerprinted_amount(
        D("500"), seed_key="k", in_use=frozenset(range(1, FINGERPRINT_SPACE + 1)),
        minimum=D("150"), maximum=None,
    ) is None
    # A plan above the policy ceiling cannot be fingerprinted into it by accident.
    assert choose_fingerprinted_amount(D("500"), seed_key="k", in_use=frozenset(),
                                       minimum=D("150"), maximum=D("400")) is None


def test_choice_never_skips_a_fingerprint_for_float_reasons():
    """The amount travels as a Decimal end-to-end, so a fingerprint whose amount
    a float could not carry (17 significant digits) is as usable as any other."""
    amount = choose_fingerprinted_amount(
        D("900000001"), seed_key="k", in_use=frozenset(range(1, FINGERPRINT_SPACE)),
        minimum=D("150"), maximum=None,
    )
    assert amount == D("900000000.99999999")
    assert fingerprint_of(amount) == FINGERPRINT_SPACE
    assert D(str(float(amount))) != amount  # what the removed float guard skipped
