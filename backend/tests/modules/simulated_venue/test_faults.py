"""Fault injection: off by default, deterministic when on."""
from __future__ import annotations

import contextlib
from decimal import Decimal

import httpx
import pytest

from bfx_funding_bot.modules.execution.submit_outcomes import (
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
from bfx_funding_bot.modules.simulated_venue.events import FaultInjected
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


# -- every injection is a durable event ----------------------------------------------------


async def _injections(w) -> list[FaultInjected]:  # type: ignore[no-untyped-def]
    return [e for e in await w.store.load(ACCOUNT) if isinstance(e, FaultInjected)]


@pytest.mark.parametrize(("target", "kind", "name"), [
    (FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST, "submit"),
    (FaultTarget.SUBMIT, FaultKind.UNKNOWN_NOT_PLACED_LOST, "submit"),
    (FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR, "submit"),
])
async def test_a_submit_injection_is_recorded_with_kind_target_ordinal_and_time(
        target: FaultTarget, kind: FaultKind, name: str) -> None:
    w = await make_world(funds={"UST": "1000"}, faults=once(target, kind, n=2))
    await w.submit_ok(amount="150")  # the first submit is not faulted
    assert await _injections(w) == []
    w.clock.advance(5_000)
    with contextlib.suppress(httpx.HTTPError):
        await w.submit(amount="150")  # the second one is
    [event] = await _injections(w)
    assert (event.fault_kind, event.target, event.request_ordinal) == (kind.value, name, 2)
    assert event.mts == w.clock.now and event.nonce > 0 and event.cid is None
    await w.submit_ok(amount="150")  # the plan is spent: nothing more is recorded
    assert len(await _injections(w)) == 1


async def test_a_history_injection_is_recorded_as_a_history_target() -> None:
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.HISTORY, FaultKind.HISTORY_ERROR))
    from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
    with pytest.raises(BitfinexAPIError):
        await w.rest.fetch_offer_history_observations(
            ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + DAY, limit=500)
    [event] = await _injections(w)
    assert (event.fault_kind, event.target, event.request_ordinal) == (
        "history_error", "history", 1)


async def test_an_injection_survives_a_restart_and_a_lost_response() -> None:
    """The event is durable before the effect: a venue rebuilt from the log still has it,
    including for 'placed but the response never arrived'."""
    w = await make_world(funds={"UST": "1000"},
                         faults=once(FaultTarget.SUBMIT, FaultKind.UNKNOWN_PLACED_LOST))
    with pytest.raises(httpx.ReadTimeout):
        await w.submit(amount="150")
    reopened = await build_simulated_venue(
        account=ACCOUNT, config=w.cfg, store=w.store, feed=w.feed, clock_ms=w.clock)
    assert [o.status for o in reopened.state.offers.values()] == ["ACTIVE"]
    [event] = await _injections(w)
    assert event.fault_kind == "unknown_placed_lost"


async def test_a_venue_without_faults_records_none() -> None:
    w = await make_world(funds={"UST": "1000"})
    await w.submit_ok(amount="150")
    assert await _injections(w) == []


async def test_the_seeded_plan_is_reproducible_and_every_firing_is_recorded() -> None:
    plan = FaultPlan(
        rules=(FaultRule(FaultTarget.SUBMIT, FaultKind.UNKNOWN_5XX_ERROR, probability=0.3),),
        seed=11)
    fired: list[list[int]] = []
    for _ in range(2):
        w = await make_world(funds={"UST": "100000"}, faults=plan)
        for _ in range(40):
            await w.submit(amount="150")
            w.clock.advance(1_000)
        fired.append([e.request_ordinal for e in await _injections(w)])
    assert fired[0] == fired[1] and 0 < len(fired[0]) < 40
