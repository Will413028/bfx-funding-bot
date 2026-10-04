"""``BFX_SIM_FAULTS``: parsed strictly, mapped onto the transport's FaultPlan, refused on Bitfinex.

Mutations (one at a time; revert after each): drop the bitfinex refusal in ``build_venue``
(``test_the_bitfinex_venue_refuses_the_fault_knob``); map ``unknown_5xx`` to another kind or
target (``test_every_knob_maps_to_its_kind_and_target``); accept an unknown name or a rate above 1
(``test_unreadable_specs_are_refused``).
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from bfx_funding_bot.apps.sim_faults import FAULT_NAMES, parse_sim_faults
from bfx_funding_bot.apps.venue import _FAULT_KNOBS, build_venue, fault_plan
from bfx_funding_bot.core.errors import ConfigurationError
from bfx_funding_bot.modules.simulated_venue import FaultKind, FaultTarget


def test_the_soak_example_parses_with_its_seed() -> None:
    spec = parse_sim_faults(
        "unknown_5xx=0.01, unknown_placed_lost=0.005,history_error=0.005,seed=7")
    assert dict(spec.rates) == {
        "unknown_5xx": 0.01, "unknown_placed_lost": 0.005, "history_error": 0.005}
    assert spec.seed == 7


def test_empty_means_no_faults() -> None:
    spec = parse_sim_faults("  ")
    assert dict(spec.rates) == {} and spec.seed == 0
    assert fault_plan(spec).rules == ()


@pytest.mark.parametrize("raw", [
    "unknown_5xx", "unknown_5xx=", "=0.1", "unknown_5xx=abc", "unknown_5xx=1.5",
    "unknown_5xx=-0.1", "unknown_5xx=nan", "nope=0.1", "unknown_5xx=0.1,unknown_5xx=0.2",
    "seed=x", "seed=-1", "seed=1,seed=2", "unknown_5xx=0.1,",
])
def test_unreadable_specs_are_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="BFX_SIM_FAULTS"):
        parse_sim_faults(raw)


@pytest.mark.parametrize(("name", "target", "kind"), [
    ("unknown_5xx", FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR),
    ("unknown_placed_lost", FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST),
    ("unknown_not_placed_lost", FaultTarget.SUBMIT, FaultKind.UNKNOWN_NOT_PLACED_LOST),
    ("history_error", FaultTarget.HISTORY, FaultKind.HISTORY_ERROR),
])
def test_every_knob_maps_to_its_kind_and_target(
        name: str, target: FaultTarget, kind: FaultKind) -> None:
    plan = fault_plan(parse_sim_faults(f"{name}=0.25,seed=3"))
    assert [(r.target, r.kind, r.probability) for r in plan.rules] == [(target, kind, 0.25)]
    assert plan.seed == 3
    assert name in FAULT_NAMES


def test_the_parsed_names_and_the_mapped_names_are_the_same_set() -> None:
    assert set(FAULT_NAMES) == set(_FAULT_KNOBS)


def test_a_zero_rate_adds_no_rule() -> None:
    assert fault_plan(parse_sim_faults("unknown_5xx=0,history_error=0.1")).rules[0].kind is (
        FaultKind.HISTORY_ERROR)
    assert len(fault_plan(parse_sim_faults("unknown_5xx=0")).rules) == 0


@pytest.mark.parametrize("config", [
    SimpleNamespace(venue="bitfinex", simulated_initial_wallets={},
                    simulated_faults={"unknown_5xx": 0.01}, simulated_fault_seed=0),
    SimpleNamespace(venue="bitfinex", simulated_initial_wallets={},
                    simulated_faults={}, simulated_fault_seed=5),
])
async def test_the_bitfinex_venue_refuses_the_fault_knob(config: SimpleNamespace) -> None:
    with pytest.raises(ConfigurationError, match="BFX_SIM_FAULTS is only valid"):
        await build_venue(
            config,  # type: ignore[arg-type]
            exchange_account_id=UUID(int=1), session_factory=None,  # type: ignore[arg-type]
            db_engine=None, bitfinex_http=None, bitfinex=None,  # type: ignore[arg-type]
            clock=lambda: 0, metrics=None,  # type: ignore[arg-type]
        )
