"""Parse tests against empirically captured API payloads (probed 2026-07-19).

Array positions verified against live responses — do NOT trust Bitfinex docs
blindly; placeholders abound.
"""
from __future__ import annotations

import pytest

from bfx_funding_bot.modules.external_signals.schemas import (
    LiquidationRecord,
    PerpFundingRecord,
)

# Live sample from GET /v2/status/deriv/tBTCF0:USTF0/hist (2026-07-19)
_DERIV_ENTRY = [
    1784471455000, None, 64493.256542075, 64453, None, 65301276.4605679,
    None, 1784476800000, 0.00047447, 7522, None, 0.00009266, None, None,
    64455.8253, None, None, 8873.99876212, None, None, None, 0.0005, 0.0025,
]

# Early-history sample (2019): several placeholder fields null, OI zero
_DERIV_ENTRY_2019 = [
    1567296058000, None, 9527.2, 9593, None, 6706.83962147,
    None, 1567324800000, -0.00001353, 19, None, 0, None, None,
    9593, None, None, 0, None, None, None, None, None,
]

# Live sample from GET /v2/liquidations/hist (2026-07-19) — inner entry
_LIQ_ENTRY = [
    "pos", 193027628, 1784428435202, None, "tETHF0:USTF0",
    -0.1, 1845.5, None, 1, 1, None, 1877.2,
]

# Initial event: is_match=0, price_acquired null
_LIQ_ENTRY_INITIAL = [
    "pos", 193045054, 1784428345140, None, "tDOGEF0:USTF0",
    1e-8, 0.072697485504, None, 0, 1, None, None,
]

# Live sample from GET /fapi/v1/fundingRate (Binance USD-M)
_BINANCE_ENTRY = {
    "symbol": "BTCUSDT",
    "fundingTime": 1784419200003,
    "fundingRate": "0.00000406",
    "markPrice": "64806.70000000",
}


class TestPerpFundingRecord:
    def test_from_bitfinex_current(self) -> None:
        rec = PerpFundingRecord.from_bitfinex(_DERIV_ENTRY, symbol="tBTCF0:USTF0")
        assert rec.venue == "bitfinex"
        assert rec.symbol == "tBTCF0:USTF0"
        assert rec.mts == 1784471455000
        assert rec.funding_rate == 0.00009266  # CURRENT_FUNDING @ [11]
        assert rec.next_funding_accrued == 0.00047447  # [8]
        assert rec.next_funding_evt_mts == 1784476800000  # [7]
        assert rec.deriv_price == 64493.256542075  # [2]
        assert rec.spot_price == 64453  # [3]
        assert rec.mark_price == 64455.8253  # [14]
        assert rec.open_interest == 8873.99876212  # [17]

    def test_from_bitfinex_2019_nulls(self) -> None:
        rec = PerpFundingRecord.from_bitfinex(_DERIV_ENTRY_2019, symbol="tBTCF0:USTF0")
        assert rec.mts == 1567296058000
        assert rec.funding_rate == 0
        assert rec.next_funding_accrued == -0.00001353
        assert rec.open_interest == 0

    def test_from_bitfinex_short_array_raises(self) -> None:
        with pytest.raises(ValueError):
            PerpFundingRecord.from_bitfinex([1784471455000, None], symbol="tBTCF0:USTF0")

    def test_from_bitfinex_null_mts_raises(self) -> None:
        bad = [None, *_DERIV_ENTRY[1:]]
        with pytest.raises(ValueError):
            PerpFundingRecord.from_bitfinex(bad, symbol="tBTCF0:USTF0")

    def test_from_binance(self) -> None:
        rec = PerpFundingRecord.from_binance(_BINANCE_ENTRY)
        assert rec.venue == "binance-usdm"
        assert rec.symbol == "BTCUSDT"
        assert rec.mts == 1784419200003
        assert rec.funding_rate == 0.00000406
        assert rec.mark_price == 64806.7
        assert rec.next_funding_accrued is None
        assert rec.open_interest is None

    def test_from_binance_empty_mark_price(self) -> None:
        raw = dict(_BINANCE_ENTRY, markPrice="")
        rec = PerpFundingRecord.from_binance(raw)
        assert rec.mark_price is None


class TestLiquidationRecord:
    def test_from_bitfinex_matched(self) -> None:
        rec = LiquidationRecord.from_bitfinex(_LIQ_ENTRY)
        assert rec.venue == "bitfinex"
        assert rec.pos_id == 193027628
        assert rec.mts == 1784428435202
        assert rec.symbol == "tETHF0:USTF0"
        assert rec.amount == -0.1  # negative = short position liquidated
        assert rec.base_price == 1845.5
        assert rec.is_match == 1
        assert rec.is_market_sold == 1
        assert rec.price_acquired == 1877.2

    def test_from_bitfinex_initial_event(self) -> None:
        rec = LiquidationRecord.from_bitfinex(_LIQ_ENTRY_INITIAL)
        assert rec.is_match == 0
        assert rec.price_acquired is None

    def test_from_bitfinex_wrong_tag_raises(self) -> None:
        bad = ["tu", *_LIQ_ENTRY[1:]]
        with pytest.raises(ValueError):
            LiquidationRecord.from_bitfinex(bad)

    def test_from_bitfinex_null_pos_id_raises(self) -> None:
        bad = list(_LIQ_ENTRY)
        bad[1] = None
        with pytest.raises(ValueError):
            LiquidationRecord.from_bitfinex(bad)

    def test_from_bitfinex_short_array_raises(self) -> None:
        with pytest.raises(ValueError):
            LiquidationRecord.from_bitfinex(["pos", 1, 2])
