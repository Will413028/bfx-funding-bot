"""Wire shapes, parsed with the REAL client code wherever it is pure or injectable."""
from __future__ import annotations

from decimal import Decimal

import pytest

from bfx_funding_bot.external.bitfinex.live_executor import (
    FundingCancelAllClient,
    classify_cancel_response,
    parse_offer_response,
)
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcomeUnknown,
    SubmitRejected,
    classify_submit_response,
)
from bfx_funding_bot.modules.execution.venue_observation import (
    normalize_credit,
    normalize_credit_history,
    normalize_offer,
    normalize_offer_history,
    normalize_trade,
    normalize_wallet,
)
from tests.modules.simulated_venue.helpers import CTX, DAY, HOUR, T0, make_world, trade

D = Decimal


async def test_active_reads_round_trip_through_the_real_parsers_and_normalizers() -> None:
    w = await make_world(funds={"UST": "1000"}, asks=[("0.0001", 2, "500")])
    first = await w.submit_ok(amount="300", rate="0.0002")
    second = await w.submit_ok(amount="200", rate="0.0009")
    w.feed.add_trades("fUST", [trade(T0 + HOUR, "600")])
    w.clock.advance(HOUR)

    wallet = [normalize_wallet(r) for r in await w.rest.fetch_wallet_observations(ctx=CTX)]
    assert [(x.currency, x.balance, x.available) for x in wallet] == [
        ("UST", D("1000"), D("1000") - D("200") - D("100") - D("200"))]  # 100 lent, 400 resting
    offers = {o.venue_offer_id: normalize_offer(o)
              for o in await w.rest.fetch_active_offer_observations(ctx=CTX)}
    assert offers[str(first)].status == "partially_filled"
    assert offers[str(first)].amount_remaining == D("200")
    assert offers[str(first)].amount_original == D("300")
    assert offers[str(second)].status == "active" and offers[str(second)].rate == D("0.0009")
    loans = [normalize_credit(r) for r in await w.rest.fetch_active_loan_observations(ctx=CTX)]
    assert [(x.amount, x.mts_opening, x.period_days) for x in loans] == [
        (D("100"), T0 + HOUR, 2)]
    assert await w.rest.fetch_active_credit_observations(ctx=CTX) == []
    only = await w.rest.fetch_active_offer_observations(ctx=CTX, symbol="fUSD")
    assert only == []


async def test_history_streams_round_trip_through_the_real_pager_and_normalizers() -> None:
    w = await make_world(funds={"UST": "1000"})
    oid = await w.submit_ok(amount="150")
    w.feed.add_trades("fUST", [trade(T0 + HOUR, "150")])
    w.clock.advance(HOUR)
    other = await w.submit_ok(amount="150", rate="0.0009")
    await w.cancel(other)
    w.clock.advance(2 * DAY)
    args = {"ctx": CTX, "symbol": "fUST", "start_ms": T0 - HOUR, "end_ms": T0 + 4 * DAY}

    offers = await w.rest.fetch_offer_history_observations(**args)
    credits = await w.rest.fetch_credit_history_observations(**args)
    loans = await w.rest.fetch_loan_history_observations(**args)
    trades = await w.rest.fetch_trade_observations(**args)
    assert all(h.complete for h in (offers, credits, loans, trades))

    by_id = {r.venue_offer_id: normalize_offer_history(r) for r in offers.rows}
    assert by_id[str(oid)].terminal_kind == "executed"
    assert by_id[str(other)].terminal_kind == "canceled"
    assert by_id[str(oid)].occurred_at_ms == T0 + HOUR  # MTS_UPDATE
    assert credits.rows == ()
    (loan,) = [normalize_credit_history(r) for r in loans.rows]
    assert loan.credit.mts_opening == T0 + HOUR and loan.occurred_at_ms == T0 + HOUR + 2 * DAY
    (tr,) = [normalize_trade(r) for r in trades.rows]
    assert (tr.venue_offer_id, tr.amount, tr.mts_create) == (str(oid), D("150"), T0 + HOUR)


async def test_legacy_pager_and_ledger_endpoints_parse() -> None:
    w = await make_world(funds={"UST": "1000"})
    await w.submit_ok(amount="150")
    w.feed.add_trades("fUST", [trade(T0 + HOUR, "150")])
    w.clock.advance(3 * DAY)
    rest = w.rest
    window = {"ctx": CTX, "start_ms": T0, "end_ms": T0 + 4 * DAY}
    payments = await rest.get_interest_payments(currency="UST", **window)
    assert len(payments) >= 2 and all(p.amount > 0 for p in payments)
    wallet = (await rest.fetch_wallet_observations(ctx=CTX))[0]
    assert payments[-1].balance <= wallet.balance  # the ledger balance trails the wallet
    assert sum(p.amount for p in payments) <= wallet.balance - D("1000")
    assert [p.mts for p in payments] == sorted(p.mts for p in payments)
    assert (await rest.get_funding_credit_history(symbol="fUST", kind="loan", **window))[0].status \
        == "CLOSED (expired)"
    assert (await rest.get_funding_trades(symbol="fUST", **window))[0].amount == D("150")


def _submit_body_ok(text_probe: list[object]) -> None:
    assert len(text_probe) == 8
    assert text_probe[1] == "fon-req"
    assert text_probe[2] is None and text_probe[3] is None and text_probe[5] is None
    assert text_probe[6] == "SUCCESS"
    assert isinstance(text_probe[7], str) and "funding offer" in text_probe[7]
    assert isinstance(text_probe[4], list) and len(text_probe[4]) == 21


async def test_submit_ack_has_the_documented_notification_shape_with_text_at_index_7() -> None:
    w = await make_world(funds={"UST": "1000"})
    response = await w.submit(amount="150")
    body = response.json()
    _submit_body_ok(body)
    outcome = classify_submit_response(response.status_code, body, True)
    assert isinstance(outcome, SubmitAcknowledged)
    assert outcome.venue_offer_id == str(body[4][0])
    assert parse_offer_response(body).venue_offer_id == outcome.venue_offer_id


async def test_business_rejection_defaults_to_5xx_error_which_the_client_reads_as_unknown() -> None:
    w = await make_world(funds={"UST": "1000"})
    response = await w.submit(amount="5000")  # more than available
    body = response.json()
    assert response.status_code == 500 and body[0] == "error" and isinstance(body[1], int)
    outcome = classify_submit_response(response.status_code, body, True)
    assert isinstance(outcome, SubmitOutcomeUnknown) and outcome.reason == "http_5xx"
    assert outcome.venue_error_code == body[1] and "balance" in (outcome.venue_error_message or "")
    assert (await w.rest.fetch_active_offer_observations(ctx=CTX)) == []  # nothing recorded


async def test_documented_200_error_rejection_form_is_a_clean_rejection() -> None:
    from tests.modules.simulated_venue.helpers import config
    w = await make_world(funds={"UST": "1000"}, cfg=config(business_rejection="200"))
    response = await w.submit(amount="5000")
    body = response.json()
    assert response.status_code == 200 and body[6] == "ERROR" and "balance" in body[7]
    outcome = classify_submit_response(200, body, True)
    assert isinstance(outcome, SubmitRejected) and outcome.reason.startswith("venue_rejected")


async def test_cancel_success_and_unknown_offer_shapes() -> None:
    w = await make_world(funds={"UST": "1000"})
    oid = await w.submit_ok(amount="150")
    ok = (await w.cancel(oid)).json()
    assert ok[1] == "foc-req" and ok[6] == "SUCCESS" and ok[4][0] == oid
    assert "CANCELED" in ok[4][10] and ok[7] == "Funding offer cancelled."
    assert classify_cancel_response(ok)[0] == "success"
    gone = (await w.cancel(oid)).json()  # no longer active
    assert gone[6] == "ERROR" and "not" in gone[7].lower()
    assert len(gone) == 8


@pytest.mark.xfail(strict=True, reason=(
    "finding: classify_cancel_response reads TEXT at index 8; Bitfinex documents "
    "[MTS, TYPE, MESSAGE_ID, null, DATA, CODE, STATUS, TEXT] with TEXT at 7, so "
    "already_terminal is never detected (fix PR fix/bitfinex-notification-text-index)"))
async def test_client_detects_already_terminal_cancel_from_the_documented_shape() -> None:
    w = await make_world(funds={"UST": "1000"})
    oid = await w.submit_ok(amount="150")
    await w.cancel(oid)
    status, text = classify_cancel_response((await w.cancel(oid)).json())
    assert status == "already_terminal" and text


@pytest.mark.xfail(strict=True, reason=(
    "finding: _structured_rejection_reason reads TEXT at index 8, so the venue's rejection "
    "text is dropped from the 200 ERROR form (same fix PR as above)"))
async def test_client_keeps_the_venue_rejection_text_from_the_documented_shape() -> None:
    from tests.modules.simulated_venue.helpers import config
    w = await make_world(funds={"UST": "1000"}, cfg=config(business_rejection="200"))
    response = await w.submit(amount="5000")
    outcome = classify_submit_response(200, response.json(), True)
    assert isinstance(outcome, SubmitRejected) and "balance" in outcome.reason


async def test_cancel_all_through_the_real_client_cancels_resting_offers_and_is_idempotent() -> None:
    w = await make_world(funds={"UST": "1000", "USD": "500"})
    await w.submit_ok(amount="150")
    await w.submit_ok(amount="150", rate="0.0003")
    await w.submit_ok(amount="150", symbol="fUSD")
    client = FundingCancelAllClient(http=w.venue.client(), auth_gate=AuthRequestGate())
    result = await client.cancel_all_funding_offers(currency="UST", ctx=CTX)
    assert result.outcome == "acknowledged" and result.venue_status == "SUCCESS"
    assert result.text == "Submitting funding offer cancellations."  # TEXT at index 7
    left = await w.rest.fetch_active_offer_observations(ctx=CTX)
    assert [o.symbol for o in left] == ["fUSD"]
    again = await client.cancel_all_funding_offers(currency="UST", ctx=CTX)
    assert again.outcome == "acknowledged"


async def test_unknown_path_host_and_method_answer_404_and_are_recorded() -> None:
    w = await make_world(funds={"UST": "1000"})
    http = w.venue.client()
    for call in (
        http.post("https://api.bitfinex.com/v2/auth/r/nonsense", content=b"{}"),
        http.post("https://api-pub.bitfinex.com/v2/auth/r/wallets", content=b"{}"),
        http.get("https://api.bitfinex.com/v2/auth/r/wallets"),
    ):
        assert (await call).status_code == 404
    assert len(w.venue.unexpected) == 3
    ok = await w.post("v2/auth/r/wallets", {})
    assert ok.status_code == 200 and len(w.venue.unexpected) == 3


async def test_funding_wallet_rows_carry_the_documented_columns() -> None:
    w = await make_world(funds={"UST": "1000"})
    (row,) = (await w.post("v2/auth/r/wallets", {})).json()
    assert row[:5] == ["funding", "UST", 1000.0, 0, 1000.0] and len(row) == 7
