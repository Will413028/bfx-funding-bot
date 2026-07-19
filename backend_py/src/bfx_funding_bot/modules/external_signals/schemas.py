"""Domain records for external signals, parsed from raw venue payloads.

Array positions verified empirically against live responses (2026-07-19):

Bitfinex /v2/status/deriv/{key}/hist entry (23 elements):
    [0] MTS  [2] DERIV_PRICE  [3] SPOT_PRICE  [5] INSURANCE_FUND_BALANCE
    [7] NEXT_FUNDING_EVT_TIMESTAMP_MS  [8] NEXT_FUNDING_ACCRUED
    [9] NEXT_FUNDING_STEP  [11] CURRENT_FUNDING  [14] MARK_PRICE
    [17] OPEN_INTEREST  [21] CLAMP_MIN  [22] CLAMP_MAX  (rest placeholders)

Bitfinex /v2/liquidations/hist inner entry (12 elements):
    [0] "pos"  [1] POS_ID  [2] MTS  [4] SYMBOL  [5] AMOUNT  [6] BASE_PRICE
    [8] IS_MATCH  [9] IS_MARKET_SOLD  [11] PRICE_ACQUIRED

Binance /fapi/v1/fundingRate entry (dict):
    {symbol, fundingTime, fundingRate, markPrice}  (markPrice may be "")
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

_DERIV_MIN_LEN = 18  # need up to index 17 (OPEN_INTEREST)
_LIQ_MIN_LEN = 12


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


class PerpFundingRecord(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    venue: str
    symbol: str
    mts: int
    funding_rate: float | None = None
    next_funding_accrued: float | None = None
    next_funding_evt_mts: int | None = None
    deriv_price: float | None = None
    spot_price: float | None = None
    mark_price: float | None = None
    open_interest: float | None = None

    @classmethod
    def from_bitfinex(cls, raw: list[Any], *, symbol: str) -> PerpFundingRecord:
        if len(raw) < _DERIV_MIN_LEN:
            raise ValueError(
                f"deriv status entry too short: {len(raw)} < {_DERIV_MIN_LEN}: {raw!r}"
            )
        if raw[0] is None:
            raise ValueError(f"deriv status entry has null MTS: {raw!r}")
        evt = raw[7]
        return cls(
            venue="bitfinex",
            symbol=symbol,
            mts=int(raw[0]),
            funding_rate=_to_float(raw[11]),
            next_funding_accrued=_to_float(raw[8]),
            next_funding_evt_mts=int(evt) if evt is not None else None,
            deriv_price=_to_float(raw[2]),
            spot_price=_to_float(raw[3]),
            mark_price=_to_float(raw[14]),
            open_interest=_to_float(raw[17]),
        )

    @classmethod
    def from_binance(cls, raw: dict[str, Any]) -> PerpFundingRecord:
        if "fundingTime" not in raw or "symbol" not in raw:
            raise ValueError(f"binance funding entry missing keys: {raw!r}")
        return cls(
            venue="binance-usdm",
            symbol=str(raw["symbol"]),
            mts=int(raw["fundingTime"]),
            funding_rate=_to_float(raw.get("fundingRate")),
            mark_price=_to_float(raw.get("markPrice")),
        )


class LiquidationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    venue: str
    pos_id: int
    mts: int
    symbol: str
    amount: float
    base_price: float | None = None
    is_match: int
    is_market_sold: int
    price_acquired: float | None = None

    @classmethod
    def from_bitfinex(cls, raw: list[Any]) -> LiquidationRecord:
        if len(raw) < _LIQ_MIN_LEN:
            raise ValueError(f"liquidation entry too short: {len(raw)}: {raw!r}")
        if raw[0] != "pos":
            raise ValueError(f"liquidation entry tag != 'pos': {raw!r}")
        if raw[1] is None or raw[2] is None:
            raise ValueError(f"liquidation entry null pos_id/mts: {raw!r}")
        amount = _to_float(raw[5])
        if amount is None:
            raise ValueError(f"liquidation entry null amount: {raw!r}")
        return cls(
            venue="bitfinex",
            pos_id=int(raw[1]),
            mts=int(raw[2]),
            symbol=str(raw[4]),
            amount=amount,
            base_price=_to_float(raw[6]),
            is_match=int(raw[8]) if raw[8] is not None else 0,
            is_market_sold=int(raw[9]) if raw[9] is not None else 0,
            price_acquired=_to_float(raw[11]),
        )
