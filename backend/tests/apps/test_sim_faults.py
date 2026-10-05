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

from bfx_funding_bot.apps.sim_faults import FAULT_KNOBS, FAULT_NAMES, parse_sim_faults
from bfx_funding_bot.apps.venue import build_venue, fault_plan
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
    "unknown_5xx_at=0", "unknown_5xx_at=-1", "unknown_5xx_at=x", "unknown_5xx_at=",
    "unknown_5xx_at=3+3", "unknown_5xx_at=3+", "unknown_5xx_at=3,unknown_5xx_at=4",
    "nope_at=3",
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


def test_one_table_names_the_knobs_and_every_entry_is_a_valid_fault() -> None:
    """The parser's names are the table's keys; every entry converts to the venue's enums
    (a typo in the table fails here, not at the first soak boot)."""
    assert tuple(FAULT_KNOBS) == FAULT_NAMES
    for name in FAULT_NAMES:
        plan = fault_plan(parse_sim_faults(f"{name}=0.5"))
        assert len(plan.rules) == 1 and plan.rules[0].target.value == FAULT_KNOBS[name][0]


def test_a_zero_rate_adds_no_rule() -> None:
    assert fault_plan(parse_sim_faults("unknown_5xx=0,history_error=0.1")).rules[0].kind is (
        FaultKind.HISTORY_ERROR)
    assert len(fault_plan(parse_sim_faults("unknown_5xx=0")).rules) == 0


@pytest.mark.parametrize("config", [
    SimpleNamespace(venue="bitfinex", simulated_initial_wallets={},
                    simulated_faults={"unknown_5xx": 0.01}, simulated_fault_seed=0,
                    simulated_fault_ordinals={}),
    SimpleNamespace(venue="bitfinex", simulated_initial_wallets={},
                    simulated_faults={}, simulated_fault_seed=5, simulated_fault_ordinals={}),
    SimpleNamespace(venue="bitfinex", simulated_initial_wallets={},
                    simulated_faults={}, simulated_fault_seed=0,
                    simulated_fault_ordinals={"unknown_5xx": (3,)}),
])
async def test_the_bitfinex_venue_refuses_the_fault_knob(config: SimpleNamespace) -> None:
    with pytest.raises(ConfigurationError, match="BFX_SIM_FAULTS is only valid"):
        await build_venue(
            config,  # type: ignore[arg-type]
            exchange_account_id=UUID(int=1), session_factory=None,  # type: ignore[arg-type]
            db_engine=None, bitfinex_http=None, bitfinex=None,  # type: ignore[arg-type]
            clock=lambda: 0, metrics=None,  # type: ignore[arg-type]
        )


def test_an_ordinal_knob_parses_alone_or_as_a_list() -> None:
    assert dict(parse_sim_faults("unknown_5xx_at=3").ordinals) == {"unknown_5xx": (3,)}
    spec = parse_sim_faults("unknown_5xx=0.01,unknown_5xx_at=7+3,history_error_at=2,seed=1")
    assert dict(spec.ordinals) == {"unknown_5xx": (3, 7), "history_error": (2,)}
    assert dict(spec.rates) == {"unknown_5xx": 0.01}


def test_fault_plan_maps_an_ordinal_knob_onto_the_rule_ordinals() -> None:
    plan = fault_plan(parse_sim_faults(
        "unknown_5xx=0.01,unknown_5xx_at=3,unknown_placed_lost=0.005,"
        "unknown_not_placed_lost_at=4,seed=2"))
    assert [(r.target, r.kind, r.probability, r.ordinals) for r in plan.rules] == [
        (FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR, 0.01, frozenset({3})),
        (FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST, 0.005, frozenset()),
        (FaultTarget.SUBMIT, FaultKind.UNKNOWN_NOT_PLACED_LOST, 0.0, frozenset({4})),
    ]


def test_the_ordinal_fires_on_the_nth_submit_of_each_process_life() -> None:
    from bfx_funding_bot.modules.simulated_venue._internal.faults import FaultInjector
    plan = fault_plan(parse_sim_faults("unknown_5xx_at=3,seed=1"))
    for _life in range(2):  # a new injector is a new process life: the ordinal restarts at 1
        injector = FaultInjector(plan)
        got = [injector.next_fault(FaultTarget.SUBMIT, nonce=10_000 + n) for n in range(1, 6)]
        assert got == [None, None, FaultKind.UNKNOWN_5XX_ERROR, None, None]
