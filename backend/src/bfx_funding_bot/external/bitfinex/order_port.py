"""What the Bitfinex funding-offer adapter takes and returns.

The venue owns these types so it imports nothing above it: the account command
gate builds an :class:`OfferRequest`, hands over only what signs the request,
and wraps the typed outcome into its own result.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from bfx_funding_bot.external.bitfinex.credentials import HasCredentials
from bfx_funding_bot.external.bitfinex.funding_rules import FundingAmountEvidence
from bfx_funding_bot.external.bitfinex.submit_outcomes import SubmitOutcome

__all__ = [
    "CancelOutcome",
    "CancelResult",
    "FundingCancelAllResult",
    "OfferRequest",
    "VenueOrderPort",
    "VenueResponseShapeError",
]


class VenueResponseShapeError(Exception):
    """The venue answered with something that is not a notification.

    Deliberately not an ``ExecutorFatalError``: no cancel handler may swallow it
    as an ordinary refusal, so it propagates to the caller.
    """


@dataclass(frozen=True, slots=True)
class OfferRequest:
    """One funding offer to submit, already valid as terms.

    Only whether this symbol is configured, the funding rule at send time and
    the final ``pre_send`` predicate remain for the adapter to decide.
    """

    symbol: str
    amount: Decimal
    rate: Decimal
    period_days: int
    funding_amount_evidence: FundingAmountEvidence | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol_missing")
        if (
            not isinstance(self.amount, Decimal) or self.amount <= 0
            or not isinstance(self.rate, Decimal) or self.rate <= 0
            or isinstance(self.period_days, bool) or not isinstance(self.period_days, int)
            or self.period_days <= 0
        ):
            raise ValueError("invalid_submit_payload")


class CancelOutcome(StrEnum):
    """How one funding-offer cancel ended at the venue."""

    SUCCESS = "success"
    # Already filled or cancelled: the requested end state holds.
    ALREADY_TERMINAL = "already_terminal"
    OTHER_ERROR = "other_error"
    TRANSIENT_EXHAUSTED = "transient_exhausted"
    FATAL = "fatal"

    @property
    def acknowledged(self) -> bool:
        return self in (CancelOutcome.SUCCESS, CancelOutcome.ALREADY_TERMINAL)


@dataclass(frozen=True, slots=True)
class CancelResult:
    outcome: CancelOutcome
    # The venue's text for a notification, or the error for a failed call.
    text: str | None = None


@dataclass(frozen=True, slots=True)
class FundingCancelAllResult:
    """The venue's answer to "cancel every funding offer in this currency".

    ``acknowledged`` means the venue accepted the request; the offers it
    cancelled are observed by the next reconcile, like any other cancel.
    ``rejected`` carries the venue's own status and text.
    """

    outcome: str  # "acknowledged" | "rejected"
    venue_status: str | None = None
    text: str | None = None


class VenueOrderPort(Protocol):
    """The funding-offer writes behind the account command gate.

    NOT_SENT contract: ``submit`` returns NOT_SENT only when it can prove no byte
    reached the venue; once the request has started, every ending is
    classified (ACKNOWLEDGED, REJECTED or UNKNOWN) and never NOT_SENT.
    """

    async def submit(
        self, offer: OfferRequest, account: HasCredentials, *,
        pre_send: Callable[[], bool] | None = None,
    ) -> SubmitOutcome: ...

    async def cancel(
        self, venue_offer_id: str, account: HasCredentials, *,
        before_transport: Callable[[], Awaitable[None]] | None = None,
    ) -> CancelResult: ...
