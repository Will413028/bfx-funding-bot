"""Exact venue records for ledger observations; statuses remain venue strings.

Raw rows retain all fields and signed amounts, with JSON decimals decoded before
parsing. These records deliberately do not normalize ledger status enums.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from bfx_funding_bot.external.bitfinex.errors import BitfinexShapeError
from bfx_funding_bot.external.bitfinex.funding_offer_row import parse_funding_offer_row


class ObservationRequestCapError(RuntimeError):
    """An active stream cannot be observed within the observation's budget."""


@dataclass(slots=True)
class ObservationRequestBudget:
    """Share one instance across all streams/symbols in one observation.

    Each attempted HTTP request consumes a slot, including failed requests.
    Active reads raise on exhaustion; covered history returns incomplete.
    """

    request_cap: int
    requests_used: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.request_cap < 0:
            raise ValueError("observation request_cap must be non-negative")

    def consume(self) -> None:
        if self.requests_used >= self.request_cap:
            raise ObservationRequestCapError("observation request cap exhausted")
        self.requests_used += 1


@dataclass(frozen=True, slots=True)
class CoveredHistory[Row]:
    rows: tuple[Row, ...]
    complete: bool
    pages: int
    requested_start_ms: int
    requested_end_ms: int


@dataclass(frozen=True, slots=True)
class WalletObservation:
    wallet_type: str
    currency: str
    balance: Decimal
    available: Decimal | None  # null means the venue has not computed it
    raw: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class OfferObservation:
    venue_offer_id: str
    symbol: str
    amount: Decimal
    amount_original: Decimal
    rate: Decimal | None
    period_days: int | None
    mts_created: int
    mts_updated: int
    status: str
    raw: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class CreditObservation:
    credit_id: str  # unprefixed venue id; identity includes source_kind
    source_kind: Literal["credit", "loan"]
    symbol: str
    side: int | None
    amount: Decimal
    rate: Decimal
    period_days: int
    mts_created: int
    mts_updated: int
    mts_opening: int
    mts_last_payout: int | None
    status: str
    raw: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class TradeObservation:
    trade_id: int
    symbol: str
    mts_create: int
    offer_id: int
    amount: Decimal
    rate: Decimal
    period_days: int
    maker: bool | None
    raw: tuple[Any, ...]


def _rows(raw: Any, *, minimum: int, required: tuple[int, ...]) -> list[list[Any]]:
    if not isinstance(raw, list):
        raise BitfinexShapeError("expected a list of observation rows")
    for row in raw:
        if (not isinstance(row, list) or len(row) < minimum
                or any(row[index] is None for index in required)):
            raise BitfinexShapeError(f"observation row malformed: {row!r}")
    return raw


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("observation numeric values must be finite")
    return result


def parse_wallet_observations(raw: Any) -> list[WalletObservation]:
    out: list[WalletObservation] = []
    for row in _rows(raw, minimum=5, required=(0, 1, 2)):
        try:
            out.append(WalletObservation(
                wallet_type=str(row[0]), currency=str(row[1]),
                balance=_decimal(row[2]),
                available=_decimal(row[4]) if row[4] is not None else None,
                raw=tuple(row),
            ))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise BitfinexShapeError(f"wallet observation has invalid values: {row!r}") from exc
    return out


def parse_offer_observations(raw: Any) -> list[OfferObservation]:
    out: list[OfferObservation] = []
    for raw_row in _rows(raw, minimum=16, required=(0, 1, 2, 3, 4, 10)):
        row = parse_funding_offer_row(raw_row)
        # Decimal abs() follows the current precision context; copy_abs() keeps
        # every wire digit, including amounts longer than that context.
        amount = _decimal(raw_row[4]).copy_abs()
        out.append(OfferObservation(
            venue_offer_id=row.venue_offer_id, symbol=row.symbol,
            amount=amount,
            amount_original=(
                _decimal(raw_row[5]).copy_abs() if raw_row[5] is not None else amount
            ),
            rate=row.rate_decimal, period_days=row.period_days,
            mts_created=row.mts_create, mts_updated=row.mts_update,
            status=row.status, raw=tuple(raw_row),
        ))
    return out


def parse_credit_observations(
    raw: Any, *, source_kind: Literal["credit", "loan"],
) -> list[CreditObservation]:
    out: list[CreditObservation] = []
    # Opening is mandatory even for active rows; never substitute MTS_CREATE.
    for row in _rows(raw, minimum=14, required=(0, 1, 3, 4, 5, 7, 11, 12, 13)):
        try:
            out.append(CreditObservation(
                credit_id=str(row[0]), source_kind=source_kind, symbol=str(row[1]),
                side=int(row[2]) if row[2] is not None else None,
                amount=_decimal(row[5]).copy_abs(), rate=_decimal(row[11]),
                period_days=int(row[12]), mts_created=int(row[3]),
                mts_updated=int(row[4]), mts_opening=int(row[13]),
                mts_last_payout=(int(row[14]) if len(row) > 14 and row[14] is not None else None),
                status=str(row[7]), raw=tuple(row),
            ))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise BitfinexShapeError(f"credit observation has invalid values: {row!r}") from exc
    return out
