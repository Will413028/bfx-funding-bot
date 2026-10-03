"""Covered pager semantics against the real `BitfinexAuthREST` with tiny pages."""
from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.modules.simulated_venue import HistoryFilter
from tests.modules.simulated_venue.helpers import CTX, DAY, HOUR, T0, World, config, make_world

D = Decimal
OFFERS = "v2/auth/r/funding/offers/fUST/hist"


async def _five_cancelled_offers(w: World, *, spacing: int = HOUR) -> list[int]:
    ids = []
    for _ in range(5):
        oid = await w.submit_ok(amount="150")
        w.clock.advance(spacing // 2)
        await w.cancel(oid)  # terminal half a spacing after creation
        w.clock.advance(spacing - spacing // 2)
        ids.append(oid)
    return ids


async def test_limit_two_covers_all_rows_newest_first_with_complete_true() -> None:
    w = await make_world(funds={"UST": "1000"})
    ids = await _five_cancelled_offers(w)
    got = await w.rest.fetch_offer_history_observations(
        ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + 10 * HOUR, limit=2, max_pages=10)
    # each page re-reads the inclusive boundary row, so five rows take five pages
    assert got.complete and got.pages == 5
    assert sorted(int(r.venue_offer_id) for r in got.rows) == sorted(ids)


async def test_a_page_budget_too_small_for_the_rows_is_reported_incomplete() -> None:
    w = await make_world(funds={"UST": "1000"})
    await _five_cancelled_offers(w)
    got = await w.rest.fetch_offer_history_observations(
        ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + 10 * HOUR, limit=2, max_pages=2)
    assert not got.complete and len(got.rows) == 3  # two overlapping pages


async def test_raw_pages_are_newest_first_by_default_and_oldest_first_with_sort_one() -> None:
    w = await make_world(funds={"UST": "1000"})
    ids = await _five_cancelled_offers(w)
    body = {"start": T0 - 1, "end": T0 + 10 * HOUR, "limit": 3}
    desc = (await w.post(OFFERS, {**body, "sort": -1})).json()
    asc = (await w.post(OFFERS, {**body, "sort": 1})).json()
    assert [r[0] for r in desc] == ids[::-1][:3]
    assert [r[0] for r in asc] == ids[:3]
    assert [r[0] for r in (await w.post(OFFERS, body)).json()] == ids[::-1][:3]


async def test_a_full_page_is_returned_while_more_rows_exist_and_end_is_inclusive() -> None:
    w = await make_world(funds={"UST": "1000"})
    ids = await _five_cancelled_offers(w)
    created = {oid: T0 + n * HOUR for n, oid in enumerate(ids)}
    page = (await w.post(OFFERS, {"start": 0, "end": created[ids[3]], "limit": 2})).json()
    assert [r[0] for r in page] == [ids[3], ids[2]]  # exactly limit rows, boundary row kept
    assert len((await w.post(OFFERS, {"start": 0, "end": T0 + DAY, "limit": 500})).json()) == 5
    assert (await w.post(OFFERS, {"start": T0 + DAY, "end": T0 + 2 * DAY, "limit": 5})).json() == []


async def test_default_limit_is_25_and_the_cap_is_500() -> None:
    w = await make_world(funds={"UST": "100000"})
    for _ in range(30):
        await w.cancel(await w.submit_ok(amount="150"))
    body = {"start": 0, "end": T0 + DAY}
    assert len((await w.post(OFFERS, body)).json()) == 25
    assert len((await w.post(OFFERS, {**body, "limit": 9999})).json()) == 30


async def test_rows_sharing_one_millisecond_fail_closed_when_a_page_cannot_hold_them() -> None:
    w = await make_world(funds={"UST": "1000"})
    ids = [await w.submit_ok(amount="150") for _ in range(3)]  # same clock tick
    for oid in ids:
        await w.cancel(oid)
    got = await w.rest.fetch_offer_history_observations(
        ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + HOUR, limit=2, max_pages=6)
    # Three rows in one millisecond, page size two: the venue serves the two highest ids
    # again and again, so the covered pager (no new id) fails closed instead of claiming
    # coverage it cannot prove. With a page that fits all three it completes.
    assert not got.complete and sorted(int(r.venue_offer_id) for r in got.rows) == ids[1:]
    wide = await w.rest.fetch_offer_history_observations(
        ctx=CTX, symbol="fUST", start_ms=T0 - 1, end_ms=T0 + HOUR, limit=4)
    assert wide.complete and len(wide.rows) == 3


@pytest.mark.parametrize(("offers_field", "expect_by"), [("create", "create"), ("update", "update")])
async def test_history_filter_field_knob_selects_which_timestamp_is_paged(
    offers_field: str, expect_by: str,
) -> None:
    cfg = config(history_filter=HistoryFilter(offers=offers_field))  # type: ignore[arg-type]
    w = await make_world(funds={"UST": "1000"}, cfg=cfg)
    oid = await w.submit_ok(amount="150")  # created at T0
    w.clock.advance(5 * HOUR)
    await w.cancel(oid)  # updated at T0 + 5h
    window_create = {"start": T0 - 1, "end": T0 + HOUR, "limit": 10}
    window_update = {"start": T0 + 4 * HOUR, "end": T0 + 6 * HOUR, "limit": 10}
    in_create = (await w.post(OFFERS, window_create)).json()
    in_update = (await w.post(OFFERS, window_update)).json()
    assert bool(in_create) == (expect_by == "create")
    assert bool(in_update) == (expect_by == "update")


async def test_history_lag_hides_a_terminal_row_until_it_has_aged() -> None:
    w = await make_world(funds={"UST": "1000"}, cfg=config(history_lag_ms=10_000))
    oid = await w.submit_ok(amount="150")
    await w.cancel(oid)
    assert (await w.rest.fetch_active_offer_observations(ctx=CTX)) == []  # gone from active
    body = {"start": 0, "end": T0 + DAY, "limit": 10}
    assert (await w.post(OFFERS, body)).json() == []  # not yet in history either
    w.clock.advance(9_999)
    assert (await w.post(OFFERS, body)).json() == []
    w.clock.advance(1)
    assert [r[0] for r in (await w.post(OFFERS, body)).json()] == [oid]


async def test_invalid_page_arguments_are_a_5xx_error_not_a_crash() -> None:
    w = await make_world(funds={"UST": "1000"})
    for body in ({"limit": "x"}, {"sort": 2}, {"start": True}):
        response = await w.post(OFFERS, body)
        assert response.status_code == 500 and response.json()[0] == "error"
