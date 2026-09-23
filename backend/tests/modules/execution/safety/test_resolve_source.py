"""resolve_for_symbol_with_source — which tier actually bound.

Motivation (2026-07-27 incident): BFX_ALLOCATION_CAP_USDT=0 was set to "pause"
the canary. Every configured symbol has an explicit caps entry, so the env
scalar bound NOTHING and the bot kept lending for hours. Reading the value told
you nothing; only knowing WHICH TIER won answers "does this knob do anything".
"""
from __future__ import annotations

from decimal import Decimal

from bfx_funding_bot.modules.execution.safety.hard_guards import (
    resolve_for_symbol,
    resolve_for_symbol_with_source,
)


def test_explicit_symbol_entry_wins_and_reports_symbol_map() -> None:
    r = resolve_for_symbol_with_source(
        {"fUST": Decimal("10000")}, "fUST",
        env_fallback=Decimal("0"), default=Decimal("5"),
    )
    assert r.value == Decimal("10000")
    assert r.source == "symbol_map"


def test_absent_symbol_falls_back_to_env_and_reports_env_fallback() -> None:
    r = resolve_for_symbol_with_source(
        {"fUST": Decimal("10000")}, "fUSD",
        env_fallback=Decimal("400"), default=Decimal("5"),
    )
    assert r.value == Decimal("400")
    assert r.source == "env_fallback"


def test_absent_symbol_without_env_reports_default() -> None:
    r = resolve_for_symbol_with_source(
        {}, "fUSD", env_fallback=None, default=Decimal("5"),
    )
    assert r.value == Decimal("5")
    assert r.source == "default"


def test_zero_env_fallback_is_still_a_binding_fallback_not_treated_as_unset() -> None:
    """Decimal('0') is falsy — a truthiness check here would silently skip the
    fallback and report `default`. The 2026-07-27 value was exactly 0."""
    r = resolve_for_symbol_with_source(
        {}, "fUSD", env_fallback=Decimal("0"), default=Decimal("5"),
    )
    assert r.value == Decimal("0")
    assert r.source == "env_fallback"


def test_legacy_resolve_delegates_to_the_sourced_one() -> None:
    """The scalar helper the guards and the reconciler call must be the SAME
    resolution, or the status report describes a tier that never bound."""
    cases = [
        ({"fUST": Decimal("10000")}, "fUST", Decimal("0"), Decimal("5")),
        ({"fUST": Decimal("10000")}, "fUSD", Decimal("400"), Decimal("5")),
        ({}, "fUSD", None, Decimal("5")),
        ({}, "fUSD", Decimal("0"), Decimal("5")),
    ]
    for mapping, symbol, env_fallback, default in cases:
        assert resolve_for_symbol(mapping, symbol, env_fallback, default) == (
            resolve_for_symbol_with_source(
                mapping, symbol, env_fallback=env_fallback, default=default,
            ).value
        )
