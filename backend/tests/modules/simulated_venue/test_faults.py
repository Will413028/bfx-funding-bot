"""Fault injection: off by default, deterministic when on."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.submit_outcomes import (
    SubmitOutcomeUnknown,
    classify_submit_response,
)
from bfx_funding_bot.modules.simulated_venue import (
    FaultKind,
    FaultPlan,
    FaultRule,
    FaultTarget,
)
from bfx_funding_bot.modules.simulated_venue._internal.faults import FaultInjector
from bfx_funding_bot.modules.simulated_venue.wiring import build_simulated_venue
from tests.modules.simulated_venue.helpers import ACCOUNT, CTX, DAY, T0, make_world

D = Decimal


def once(target: FaultTarget, kind: FaultKind, n: int = 1) -> FaultPlan:
    return FaultPlan(rules=(FaultRule(target, kind, ordinals=frozenset({n})),))


async def _active_ids(w) -> list[str]:  # type: ignore[no-untyped-def]
    return [o.venue_offer_id for o in await w.rest.fetch_active_offer_observations(ctx=CTX)]


def test_faults_are_off_by_default_everywhere() -> None:
    assert FaultPlan().rules == ()
    injector = FaultInjector(FaultPlan())
    assert not injector.enabled
    assert all(injector.next_fault(t) is None for t in FaultTarget for _ in range(50))


async def test_a_venue_built_without_a_plan_never_injects_anything() -> None:
    w = await make_world(funds={"UST": "100000"})
    for _ in range(40):
        oid = await w.submit_ok(amount="150")
        assert (await w.cancel(oid)).json()[6] == "SUCCESS"
    got = await w.rest.fetch_offer_history_observations(
        ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + DAY, limit=500)
    assert got.complete and len(got.rows) == 40


async def test_placed_but_response_lost_is_durable_and_reads_as_unknown() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST))
    with pytest.raises(httpx.ReadTimeout) as caught:
        await w.submit(amount="150")
    outcome = classify_submit_response(exception=caught.value, transport_started=True)
    assert isinstance(outcome, SubmitOutcomeUnknown) and outcome.reason == "timeout"
    assert len(await _active_ids(w)) == 1  # the offer exists although the caller never heard
    again = await w.submit_ok(amount="150")  # the fault was one-shot
    assert len(await _active_ids(w)) == 2 and again


async def test_lost_before_the_venue_never_places_and_reads_as_unknown() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.SUBMIT, FaultKind.UNKNOWN_NOT_PLACED_LOST))
    with pytest.raises(httpx.ConnectError) as caught:
        await w.submit(amount="150")
    outcome = classify_submit_response(exception=caught.value, transport_started=True)
    assert isinstance(outcome, SubmitOutcomeUnknown) and outcome.reason == "connection_error"
    assert await _active_ids(w) == []


async def test_realistic_rejection_is_5xx_with_error_body_and_places_nothing() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR))
    response = await w.submit(amount="150")
    body = response.json()
    assert response.status_code == 500 and body[0] == "error" and isinstance(body[1], int)
    outcome = classify_submit_response(response.status_code, body, True)
    assert isinstance(outcome, SubmitOutcomeUnknown) and outcome.venue_error_code == body[1]
    assert await _active_ids(w) == []


async def test_a_key_refused_on_submit_is_the_revoked_key_answer_and_places_nothing() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.SUBMIT, FaultKind.KEY_REFUSED))
    response = await w.submit(amount="150")
    assert response.status_code == 500 and response.json() == ["error", 10100, "apikey: invalid"]
    outcome = classify_submit_response(response.status_code, response.json(), True)
    assert isinstance(outcome, SubmitOutcomeUnknown) and outcome.venue_error_code == 10100
    assert await _active_ids(w) == []


async def test_documented_rejection_is_a_200_error_notification_and_places_nothing() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.SUBMIT, FaultKind.REJECTED))
    response = await w.submit(amount="150")
    body = response.json()
    assert response.status_code == 200 and body[6] == "ERROR" and len(body) == 8
    assert await _active_ids(w) == []


async def test_cancel_faults_apply_to_cancel_only() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.CANCEL, FaultKind.UNKNOWN_PLACED_LOST))
    oid = await w.submit_ok(amount="150")
    with pytest.raises(httpx.ReadTimeout):
        await w.cancel(oid)
    assert await _active_ids(w) == []  # the cancel took effect; the caller does not know


async def test_incomplete_history_omits_the_newest_row_silently() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.HISTORY, FaultKind.HISTORY_OMIT_NEWEST))
    ids = []
    for _ in range(3):
        oid = await w.submit_ok(amount="150")
        await w.cancel(oid)
        ids.append(oid)
        w.clock.advance(1000)
    got = await w.rest.fetch_offer_history_observations(
        ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + DAY, limit=10)
    assert got.complete  # the client cannot tell: that is the point of the fault
    assert sorted(int(r.venue_offer_id) for r in got.rows) == ids[:2]


async def test_history_error_makes_the_covered_pager_certify_nothing() -> None:
    from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.HISTORY, FaultKind.HISTORY_ERROR))
    with pytest.raises(BitfinexAPIError) as caught:
        await w.rest.fetch_offer_history_observations(
            ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + DAY)
    assert caught.value.status_code == 503


def test_probabilistic_rules_are_seeded_and_reproducible() -> None:
    rule = FaultRule(FaultTarget.SUBMIT, FaultKind.REJECTED, probability=0.3)

    def run(seed: int) -> list[FaultKind | None]:
        inj = FaultInjector(FaultPlan(rules=(rule,), seed=seed))
        return [inj.next_fault(FaultTarget.SUBMIT) for _ in range(200)]

    assert run(7) == run(7)
    assert run(7) != run(8)
    fired = sum(1 for f in run(7) if f is not None)
    assert 30 < fired < 90
    inj = FaultInjector(FaultPlan(rules=(rule,), seed=7))
    assert all(inj.next_fault(FaultTarget.CANCEL) is None for _ in range(50))  # other targets


@pytest.mark.parametrize("make", [
    lambda: FaultRule(FaultTarget.SUBMIT, FaultKind.HISTORY_ERROR, ordinals=frozenset({1})),
    lambda: FaultRule(FaultTarget.HISTORY, FaultKind.REJECTED, ordinals=frozenset({1})),
    lambda: FaultRule(FaultTarget.SUBMIT, FaultKind.REJECTED),
    lambda: FaultRule(FaultTarget.SUBMIT, FaultKind.REJECTED, ordinals=frozenset({0})),
    lambda: FaultRule(FaultTarget.SUBMIT, FaultKind.REJECTED, probability=1.5),
    lambda: FaultRule(FaultTarget.SUBMIT, FaultKind.TICK_AFTER, ordinals=frozenset({1})),
    lambda: FaultRule(FaultTarget.ANY_REQUEST, FaultKind.REJECTED, ordinals=frozenset({1})),
    lambda: FaultRule(FaultTarget.SUBMIT, FaultKind.REJECTED, ordinals=frozenset({1}),
                      hook=lambda: None),
])
def test_invalid_fault_rules_are_refused(make) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        make()


def test_tick_rules_are_part_of_the_one_plan_and_count_every_request() -> None:
    fired: list[int] = []
    rule = FaultRule(FaultTarget.ANY_REQUEST, FaultKind.TICK_AFTER,
                     ordinals=frozenset({2, 4}), hook=lambda: fired.append(1))
    plan = FaultPlan(rules=(rule,))
    inj = FaultInjector(plan)
    assert inj.enabled
    assert [bool(inj.ticks_after(n)) for n in range(1, 6)] == [False, True, False, True, False]
    assert plan == FaultPlan(rules=(FaultRule(
        FaultTarget.ANY_REQUEST, FaultKind.TICK_AFTER, ordinals=frozenset({2, 4})),))  # hook ignored


# -- a placed-but-lost submit is durable ------------------------------------------------------


async def test_a_placed_but_lost_submit_survives_a_restart() -> None:
    """'Placed but the response never arrived': a venue rebuilt from the log has the offer."""
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST))
    with pytest.raises(httpx.ReadTimeout):
        await w.submit(amount="150")
    reopened = await build_simulated_venue(
        account=ACCOUNT, config=w.cfg, store=w.store, feed=w.feed, clock_ms=w.clock)
    assert [o.status for o in reopened.state.offers.values()] == ["ACTIVE"]


def test_probability_draws_follow_the_nonce_so_a_restart_never_replays_them() -> None:
    """The process ordinal restarts at 1 with every process; the venue's nonce only grows."""
    plan = FaultPlan(
        rules=(FaultRule(FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR, probability=0.5),),
        seed=7)

    def draws(nonces: range) -> list[bool]:
        injector = FaultInjector(plan)  # a new process: its ordinal starts at 1
        return [injector.next_fault(FaultTarget.SUBMIT, nonce=n) is not None for n in nonces]

    first_life, second_life = draws(range(1_000, 1_200)), draws(range(2_000, 2_200))
    assert first_life != second_life  # same seed, same ordinals 1..200, different nonces
    assert draws(range(1_000, 1_200)) == first_life  # the same nonce always draws the same


def test_ordinal_rules_still_fire_on_the_process_ordinal() -> None:
    injector = FaultInjector(once(FaultTarget.SUBMIT, FaultKind.REJECTED, n=2))
    assert [injector.next_fault(FaultTarget.SUBMIT, nonce=9_000 + i) for i in range(3)] == [
        None, FaultKind.REJECTED, None]


async def test_a_venue_reopened_on_the_same_log_draws_new_faults() -> None:
    """End to end: two process lives of one venue with the same seed do not repeat a pattern."""
    plan = FaultPlan(
        rules=(FaultRule(FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR, probability=0.4),),
        seed=5)
    w = await make_world(funds={"UST": "100000"}, faults=plan)
    first = []
    for _ in range(30):
        first.append((await w.submit(amount="150")).status_code == 500)
        w.clock.advance(1_000)
    reopened = await build_simulated_venue(
        account=ACCOUNT, config=w.cfg, store=w.store, feed=w.feed, clock_ms=w.clock, faults=plan)
    w.venue = reopened
    second = []
    for _ in range(30):
        second.append((await w.submit(amount="150")).status_code == 500)
        w.clock.advance(1_000)
    assert first != second
