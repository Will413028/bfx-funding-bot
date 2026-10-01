"""Offline shadow contracts and legacy wire/result parity."""
import json
import logging
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest

from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.errors import BitfinexAPIError
from bfx_funding_bot.modules.execution.protocols import AccountContext, Credentials
from bfx_funding_bot.modules.execution.venue_normalization_shadow import (
    VenueNormalizationShadow,
    classify_path,
)

LOGGER = "bfx_funding_bot.venue_normalization_shadow"
BASE = "v2/auth/r/"
CTX = AccountContext("test", Credentials("TEST_KEY", "TEST_SECRET"), Decimal("1000"))


def offer(status="ACTIVE", row_id=10):
    return [row_id, "fUST", 100, 200, "-4.30000000000000004", "-10.3", "LIMIT",
            None, None, 64, status, None, None, None, "0.0003", 2]


def credit(status="ACTIVE", opening=50):
    return [20, "fUST", 1, 100, 200, "-6.00000000000000004", 0, status, "FIXED",
            None, None, "0.0003", 2, opening, 200]


def observe(shadow, suffix, rows):
    shadow(BASE + suffix, json.dumps(rows).encode())


def warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


@pytest.mark.parametrize("kind", ["offers", "credits", "loans"])
@pytest.mark.parametrize("symbol", ["", "/fUST"])
@pytest.mark.parametrize("hist", ["", "/hist"])
def test_path_table(kind, symbol, hist):
    assert classify_path(BASE + "funding/" + kind + symbol + hist) == (
        kind + ("_history" if hist else ""))


@pytest.mark.parametrize("suffix,expected", [
    ("wallets", "wallets"), ("funding/trades/fUST/hist", "trades"),
    ("funding/trades/hist", "trades"), ("funding/trades", None),
    ("ledgers/UST/hist", None), ("permissions", None),
    ("funding/offers/fUST/hist/extra", None), ("funding/offers/fUST/extra", None),
])
def test_other_paths(suffix, expected):
    assert classify_path(BASE + suffix) == expected


@pytest.mark.parametrize("path", [
    "v2/auth/w/funding/offer/submit", "v2/auth/w/funding/offer/cancel",
    "v2/auth/w/funding/offer/cancel/all", "v2/auth/w/funding/offers",
    "v2/auth/w/funding/credits/fUST/hist",
])
def test_write_responses_are_never_observed(path):
    assert classify_path(path) is None


@pytest.mark.parametrize("suffix,rows", [
    ("funding/offers", [offer(), offer("PARTIALLY FILLED @ 0.03")]),
    ("funding/offers/fUST/hist", [offer("EXECUTED at 0.03"), offer("CANCELED"), offer("EXPIRED")]),
    ("funding/credits", [credit()]), ("funding/loans", [credit()]),
    ("funding/credits/fUST/hist", [credit("CLOSED (no more position)")]),
    ("funding/loans/fUST/hist", [credit("CLOSED")]),
    ("funding/trades/fUST/hist", [[30, "fUST", 100, 10, "4.3", "0.0003", 2, 1]]),
    ("wallets", [["funding", "UST", "10.3", None, "4.3"]]),
])
def test_known_rows_are_silent(suffix, rows, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    observe(VenueNormalizationShadow(), suffix, rows)
    assert not caplog.records


def test_unknown_status_dedup_head_and_per_row(caplog):
    shadow = VenueNormalizationShadow()
    observe(shadow, "funding/offers", [offer("EXECUTED at 0.03"), offer("EXECUTED at 0.04")])
    observe(shadow, "funding/offers", [offer("EXECUTED at 0.05")])
    assert len(warnings(caplog)) == 1
    assert "unknown_status" in warnings(caplog)[0].message
    assert "row_id=10" in warnings(caplog)[0].message
    assert shadow._counts == {("offers", "unknown_status", "EXECUTED"): 3}
    observe(shadow, "funding/offers", [[99], offer("MYSTERY")])
    assert len(warnings(caplog)) == 3


def test_missing_opening_and_funding_null_only(caplog):
    shadow = VenueNormalizationShadow()
    observe(shadow, "funding/credits", [credit(opening=None)])
    observe(shadow, "wallets", [["exchange", "UST", 10, None, None]])
    assert len(warnings(caplog)) == 1
    observe(shadow, "wallets", [["funding", "UST", 10, None, None]])
    assert len(warnings(caplog)) == 2
    assert "stream=credits reason=parse_error" in warnings(caplog)[0].message
    assert "stream=wallets reason=normalization_error" in warnings(caplog)[1].message


@pytest.mark.parametrize("body,reason", [(b'{', "invalid_json"), (b'{}', "non_list_body"),
                                         (b'\xff', "invalid_json")])
def test_bad_body_reported_once(body, reason, caplog):
    shadow = VenueNormalizationShadow()
    shadow(BASE + "funding/offers", body)
    shadow(BASE + "funding/offers", body)
    assert len(warnings(caplog)) == 1
    assert reason in warnings(caplog)[0].message


def test_key_cap_and_hourly_cumulative_summary(caplog):
    now = [0.0]
    shadow = VenueNormalizationShadow(clock=lambda: now[0])
    caplog.set_level(logging.INFO, logger=LOGGER)
    for i in range(130):
        observe(shadow, "funding/offers", [offer(f"UNKNOWN{i}")])
    assert len(shadow._counts) == 128 and shadow._overflow == 2
    assert len(warnings(caplog)) == 128
    observe(shadow, "funding/offers", [offer("UNKNOWN0")])
    assert shadow._counts[("offers", "unknown_status", "UNKNOWN0")] == 2
    now[0] = 3599
    observe(shadow, "wallets", [])
    assert not [r for r in caplog.records if r.levelno == logging.INFO]
    now[0] = 3600
    observe(shadow, "wallets", [])
    summaries = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(summaries) == 1 and "overflow=2" in summaries[0].message
    observe(shadow, "wallets", [])
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1


def test_status_truncated_and_decimal_preserved(caplog, monkeypatch):
    seen = []
    monkeypatch.setattr("bfx_funding_bot.modules.execution.venue_normalization_shadow.normalize_offer",
                        lambda row: seen.append(row))
    shadow = VenueNormalizationShadow()
    shadow(BASE + "funding/offers", json.dumps([offer()]).replace(
        '"-4.30000000000000004"', '-4.30000000000000004').encode())
    assert seen[0].amount == Decimal("4.30000000000000004")
    monkeypatch.undo()
    status = "UNKNOWN " + "x" * 250
    observe(shadow, "funding/offers", [offer(status)])
    assert status[:200] in warnings(caplog)[0].message
    assert status[:201] not in warnings(caplog)[0].message


async def legacy_reads(rest):
    return [
        await rest.get_active_funding_offers(ctx=CTX),
        await rest.get_active_funding_credits(ctx=CTX),
        await rest.get_active_funding_loans(ctx=CTX),
        await rest.get_funding_available_all(ctx=CTX),
        await rest.get_funding_offer_history(ctx=CTX, start_ms=0, end_ms=1000),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_kind", ["shadow", "raising", "logging_failure", "shadow_logging_failure"])
async def test_legacy_results_and_wire_sequence_identical(observer_kind, monkeypatch):
    bodies = [json.dumps(rows).encode() for rows in [
        [offer()], [credit()], [credit()], [["funding", "UST", 10, None, None]],
        [offer("EXECUTED at 0.03")],
    ]]
    async def run(observer):
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path, request.content))
            return httpx.Response(200, content=bodies[len(calls) - 1])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            results = await legacy_reads(BitfinexAuthREST(http=http, response_observer=observer))
        return results, calls
    baseline = await run(None)
    seen = []
    shadow = VenueNormalizationShadow()
    def observer(path, content):
        assert type(content) is bytes
        seen.append((path, content))
        if observer_kind.startswith("shadow"):
            shadow(path, content)
        else:
            raise RuntimeError("broken observer")
    if observer_kind == "logging_failure":
        monkeypatch.setattr("bfx_funding_bot.external.bitfinex.auth_rest.log.warning",
                            Mock(side_effect=RuntimeError("broken logger")))
    if observer_kind == "shadow_logging_failure":
        monkeypatch.setattr("bfx_funding_bot.modules.execution.venue_normalization_shadow.log.warning",
                            Mock(side_effect=RuntimeError("broken shadow logger")))
    assert await run(observer) == baseline
    assert [content for _, content in seen] == bodies
    assert ["/" + path for path, _ in seen] == [call[1] for call in baseline[1]]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["http", "transport"])
async def test_failed_requests_never_observed(error):
    observer = Mock()
    def handler(request):
        if error == "transport":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(403, content=b'["error", 10000, "denied"]')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(BitfinexAPIError) as exc:
            await BitfinexAuthREST(http=http, response_observer=observer).get_active_funding_offers(
                ctx=CTX)
    assert exc.value.status_code == (403 if error == "http" else 0)
    observer.assert_not_called()


def residual_shadow(now, symbols=("UST",)):
    shadow = VenueNormalizationShadow(clock=lambda: now[0])
    for stream, factory in (("offers", offer), ("credits", credit), ("loans", credit)):
        rows = []
        for symbol in symbols:
            row = factory()
            row[1] = "f" + symbol
            row[4 if stream == "offers" else 5] = "2" if stream == "offers" else "3"
            rows.append(row)
        observe(shadow, "funding/" + stream, rows)
    return shadow


def test_wallet_residual_zero_and_hourly_summary(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    now = [0.0]
    shadow = residual_shadow(now)
    observe(shadow, "wallets", [["funding", "UST", "10", None, "2"]])
    assert not warnings(caplog)
    assert shadow._residuals["UST"].samples == 1
    now[0] = 3600
    observe(shadow, "wallets", [])
    summaries = [r.message for r in caplog.records if "wallet_residual_summary" in r.message]
    assert len(summaries) == 1
    assert "samples=1 max_abs=0 last=0 above_tolerance=0 stale=0" in summaries[0]
    observe(shadow, "wallets", [])
    assert len([r for r in caplog.records if "wallet_residual_summary" in r.message]) == 1


def test_wallet_residual_warning_dedup(caplog):
    shadow = residual_shadow([0.0])
    for _ in range(3):
        observe(shadow, "wallets", [["funding", "UST", "11", None, "2"]])
    assert len(warnings(caplog)) == 1
    assert warnings(caplog)[0].message == "venue_wallet_residual currency=UST residual=1"
    stats = shadow._residuals["UST"]
    assert (stats.samples, stats.max_abs, stats.last, stats.above_tolerance) == (3, 1, 1, 3)


@pytest.mark.parametrize("stream", ["offers", "credits", "loans"])
def test_wallet_residual_stale_snapshot_skipped(stream, caplog):
    now = [0.0]
    shadow = residual_shadow(now)
    now[0] = 30
    observe(shadow, "wallets", [["funding", "UST", "10", None, "2"]])
    now[0] = 31
    for fresh in {"offers", "credits", "loans"} - {stream}:
        observe(shadow, "funding/" + fresh, [])
    observe(shadow, "wallets", [["funding", "UST", "99", None, "2"]])
    stats = shadow._residuals["UST"]
    assert (stats.samples, stats.stale, stats.last) == (1, 1, 0)
    assert not warnings(caplog)


def test_wallet_residual_multiple_currencies_and_exchange_ignored(caplog):
    shadow = residual_shadow([0.0], ("UST", "USD"))
    observe(shadow, "wallets", [
        ["funding", "UST", "10", None, "2"],
        ["funding", "USD", "12", None, "2"],
        ["exchange", "BTC", "100", None, "0"],
    ])
    assert set(shadow._residuals) == {"UST", "USD"}
    assert shadow._residuals["UST"].last == 0
    assert shadow._residuals["USD"].last == 2
    assert len(warnings(caplog)) == 1


def test_wallet_residual_parse_failure_skips_row_and_history_keeps_snapshot(caplog):
    shadow = residual_shadow([0.0])
    observe(shadow, "funding/loans", [[99], credit()])
    observe(shadow, "funding/offers/fUST/hist", [offer("EXECUTED")])
    observe(shadow, "wallets", [["funding", "UST", "13.00000000000000004", None, "2"]])
    assert shadow._residuals["UST"].last == 0
    assert len(warnings(caplog)) == 1
    assert "parse_error" in warnings(caplog)[0].message


def test_wallet_residual_missing_snapshot_skipped():
    shadow = VenueNormalizationShadow()
    observe(shadow, "wallets", [["funding", "UST", "10", None, "2"]])
    assert shadow._residuals["UST"].stale == 1
    assert shadow._residuals["UST"].samples == 0


def test_wallet_residual_tolerance_negative_and_empty_snapshot_replaces(caplog):
    shadow = residual_shadow([0.0])
    observe(shadow, "wallets", [["funding", "UST", "10.00000001", None, "2"]])
    assert not warnings(caplog)
    observe(shadow, "wallets", [["funding", "UST", "9.99999998", None, "2"]])
    assert len(warnings(caplog)) == 1
    observe(shadow, "funding/offers", [])
    observe(shadow, "wallets", [["funding", "UST", "8", None, "2"]])
    stats = shadow._residuals["UST"]
    assert stats.samples == 3
    assert stats.max_abs == Decimal("0.00000002")
    assert stats.last == 0
    assert stats.above_tolerance == 1


def test_wallet_residual_per_symbol_read_does_not_replace_snapshot(caplog):
    """A per-symbol offers read is partial; only all-symbol reads are snapshots."""
    shadow = residual_shadow([0.0], symbols=("UST", "USD"))
    observe(shadow, "funding/offers/fUSD", [])
    observe(shadow, "wallets", [["funding", "UST", "10", None, "2"]])
    assert not warnings(caplog)
    assert shadow._residuals["UST"].samples == 1
