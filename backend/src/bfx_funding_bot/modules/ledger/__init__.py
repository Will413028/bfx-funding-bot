"""Dormant, transaction-scoped ledger journal, observation, and read contracts."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.trading import (
    AppliedPolicy,
    CapitalBudget,
    CapitalResult,
    CapitalScope,
    CapitalSnapshot,
)

type JsonObject = dict[str, object]
type OutcomeKind = Literal["ack", "rejected", "not_sent", "unknown"]
type ResolutionAction = Literal["bound_to_venue", "not_accepted", "manual"]
type CreditKind = Literal["credit", "loan"]
type AcceptanceDecision = Literal["accepted", "fenced", "incomplete_or_unequal"]

# Closed status vocabularies. The port normalizes Bitfinex strings into these;
# ledger never parses venue text.
#   offer status       "ACTIVE" -> active; "PARTIALLY FILLED ..." -> partially_filled
#                      (a history row keeps the last of these it had)
#   offer terminal     "EXECUTED at r% (a)" -> executed;
#                      "CANCELED", "PARTIALLY FILLED at r% (a), CANCELED" -> canceled
#   credit/loan status "ACTIVE" -> active
#   credit terminal    "CLOSED (expired)", "CLOSED (closed)", "CLOSED (reduced)" -> closed
type OfferStatus = Literal["active", "partially_filled"]
type CreditStatus = Literal["active"]
type OfferTerminalKind = Literal["executed", "canceled"]
type CreditTerminalKind = Literal["closed"]
OFFER_STATUSES: frozenset[str] = frozenset(("active", "partially_filled"))
CREDIT_STATUSES: frozenset[str] = frozenset(("active",))
OFFER_TERMINAL_KINDS: frozenset[str] = frozenset(("executed", "canceled"))
CREDIT_TERMINAL_KINDS: frozenset[str] = frozenset(("closed",))


@dataclass(frozen=True, slots=True)
class Wallet:
    wallet_type: str
    currency: str
    available: Decimal
    balance: Decimal
    # Funding symbol assigned by the port (e.g. fUST); None only for non-funding
    # wallets. No default: a funding wallet without a symbol would drop silently.
    symbol: str | None


@dataclass(frozen=True, slots=True)
class Offer:
    venue_offer_id: str
    symbol: str
    amount_original: Decimal | None
    amount_remaining: Decimal
    rate: Decimal | None
    rate_observed: bool
    period_days: int | None
    offer_type: str | None
    flags: JsonObject | int | None
    status: OfferStatus
    mts_created: int
    mts_updated: int | None
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class Credit:
    source_kind: CreditKind
    venue_credit_id: str
    symbol: str
    amount: Decimal
    rate: Decimal | None
    period_days: int | None
    status: CreditStatus
    flags: JsonObject | int | None
    mts_created: int | None
    mts_updated: int | None
    mts_opening: int  # the venue's trade instant; required (carry key)
    raw: JsonObject


@dataclass(frozen=True, slots=True)
class OfferHistory:
    offer: Offer
    terminal_kind: OfferTerminalKind
    occurred_at_ms: int


@dataclass(frozen=True, slots=True)
class CreditHistory:
    credit: Credit
    terminal_kind: CreditTerminalKind
    occurred_at_ms: int


@dataclass(frozen=True, slots=True)
class Trade:
    """A funding trade (one of the account's offers matched); amount is absolute."""

    trade_id: int
    symbol: str
    venue_offer_id: str
    amount: Decimal
    rate: Decimal
    period_days: int
    mts_create: int
    maker: bool | None = None


@dataclass(frozen=True, slots=True)
class Coverage:
    wallets_complete: bool
    offers_complete: bool
    credits_complete: bool
    loans_complete: bool
    offer_history_complete: bool
    credit_history_complete: bool
    wallet_pages: int
    offer_pages: int
    credit_pages: int
    loan_pages: int
    offer_history_pages: int
    credit_history_pages: int
    history_requested_start_ms: int | None = None
    history_requested_end_ms: int | None = None
    history_oldest_mts_created: int | None = None
    history_newest_mts_created: int | None = None
    trades_complete: bool = False
    trades_requested_start_ms: int | None = None
    trades_requested_end_ms: int | None = None

    @property
    def active_complete(self) -> bool:
        return all(
            (
                self.wallets_complete,
                self.offers_complete,
                self.credits_complete,
                self.loans_complete,
            )
        )

    @property
    def complete(self) -> bool:
        return all(
            (
                self.active_complete,
                self.offer_history_complete,
                self.credit_history_complete,
                self.trades_complete,
            )
        )


@dataclass(frozen=True, slots=True)
class Observation:
    wallets: tuple[Wallet, ...]
    offers: tuple[Offer, ...]
    credits: tuple[Credit, ...]
    coverage: Coverage
    finished_at_ms: int
    offer_history: tuple[OfferHistory, ...] = ()
    credit_history: tuple[CreditHistory, ...] = ()
    trades: tuple[Trade, ...] = ()


@dataclass(frozen=True, slots=True)
class Acceptance:
    decision: AcceptanceDecision
    observation_id: UUID | None
    first_digest: str | None
    confirmation_digest: str | None


@dataclass(frozen=True, slots=True)
class Scope:
    exchange_account_id: UUID
    deployment_environment: str


@dataclass(frozen=True, slots=True)
class QueryHandle:
    query_id: UUID
    query_revision: int
    start_revision: int
    started_at_ms: int


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: UUID
    execution_decision_id: str
    symbol: str
    cell_id: str  # must equal the decision's cell (enforced by the scope trigger)
    normalized_payload: JsonObject
    basis_id: UUID
    policy_revision_id: UUID
    authorization_evidence: JsonObject
    started_at_ms: int
    seed_provenance: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class RecordedAttempt:
    attempt_id: UUID
    attempt_seq: int
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class Outcome:
    attempt_id: UUID
    kind: OutcomeKind
    venue_offer_id: str | None
    reason: str | None
    completed_at_ms: int
    evidence: JsonObject


@dataclass(frozen=True, slots=True)
class Resolution:
    id: UUID
    symbol: str
    action: ResolutionAction
    venue_offer_id: str | None
    observation_id: UUID
    actor_kind: str
    actor_id: str
    resolved_at_ms: int
    reason: str
    evidence: JsonObject
    attempt_id: UUID | None = None
    quarantine_id: UUID | None = None
    operator_request_id: UUID | None = None
    candidate_count: int | None = None


@dataclass(frozen=True, slots=True)
class Quarantine:
    quarantine_id: UUID
    symbol: str
    intended_amount: Decimal
    opened_at_ms: int
    evidence: JsonObject
    legacy_reconcile_event_seq: int | None = None


@dataclass(frozen=True, slots=True)
class QuarantineMember:
    quarantine_id: UUID
    source_kind: Literal["offer", "credit", "loan"]
    venue_object_id: str
    observation_id: UUID
    amount_at_join: Decimal


class OutcomeAlreadyRecorded(ValueError):  # noqa: N818 - named by journal contract
    def __init__(self, stored: Outcome) -> None:
        self.stored = stored
        super().__init__("outcome already recorded")


class ResolutionAlreadyRecorded(ValueError):  # noqa: N818 - named by journal contract
    def __init__(self, stored: Resolution) -> None:
        self.stored = stored
        super().__init__("resolution already recorded")


class QuarantineMemberConflict(ValueError):  # noqa: N818 - named by journal contract
    def __init__(self, stored: QuarantineMember) -> None:
        self.stored = stored
        super().__init__("quarantine member already recorded")


class ResolutionRejected(ValueError):  # noqa: N818 - named by journal contract
    """Structural subject or observation precondition failed."""


class QueryAdmissionRefused(ValueError):  # noqa: N818 - named by query contract
    """An in-flight command makes a pre-I/O fence unsafe."""


class LedgerJournal(Protocol):
    async def bump_clock(self, session: AsyncSession, scope: Scope) -> int: ...
    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle: ...
    async def record_attempt(
        self, session: AsyncSession, scope: Scope, attempt: Attempt
    ) -> RecordedAttempt: ...
    async def record_outcome(
        self, session: AsyncSession, scope: Scope, outcome: Outcome
    ) -> None: ...
    async def read_back_outcome(
        self, session: AsyncSession, attempt_id: UUID
    ) -> Outcome | None: ...
    async def record_resolution(
        self, session: AsyncSession, scope: Scope, resolution: Resolution
    ) -> None: ...
    async def open_quarantine(
        self, session: AsyncSession, scope: Scope, quarantine: Quarantine
    ) -> None: ...
    async def add_quarantine_member(
        self, session: AsyncSession, scope: Scope, member: QuarantineMember
    ) -> None: ...


class CapitalReadRefused(ValueError):  # noqa: N818 - named by reader contract
    """The caller's transaction is not REPEATABLE READ READ ONLY."""


@dataclass(frozen=True, slots=True)
class LedgerCapitalRead:
    """``trading.derive_capital``'s result and the basis it folded (None if none)."""

    result: CapitalResult
    basis_id: UUID | None


class LedgerCapitalReader(Protocol):
    """Read one symbol/cell's capital from the latest accepted basis and its tail.

    The caller opens and holds one REPEATABLE READ READ ONLY transaction on
    ``session`` (anything else raises ``CapitalReadRefused``); ``now_ms`` is
    the caller's local clock, compared with the query's local start time.
    """

    async def read_capital(
        self,
        session: AsyncSession,
        scope: CapitalScope,
        *,
        now_ms: int,
        max_snapshot_age_ms: int,
    ) -> LedgerCapitalRead: ...


class LedgerObservations(Protocol):
    """Commit a query fence, then accept its two reads (an accepted one writes the basis).

    ``begin_query`` runs in its own committed transaction before any venue I/O;
    ``accept`` runs in a later transaction on the caller's session.
    """

    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle: ...
    async def accept(
        self,
        session: AsyncSession,
        scope: Scope,
        handle: QueryHandle,
        first: Observation,
        confirmation: Observation,
        confirmation_started_at_ms: int,
    ) -> Acceptance: ...


class LedgerReadUnbounded(RuntimeError):  # noqa: N818 - fail-closed read refusal
    """The attempt tail since the latest accepted basis exceeds its cap."""


class ProvenanceConflict(ValueError):  # noqa: N818 - named by the read contract
    """More than one story (attempt, scope or symbol) for one venue offer."""

    def __init__(self, venue_offer_id: str) -> None:
        self.venue_offer_id = venue_offer_id
        super().__init__(f"contradictory provenance for venue offer {venue_offer_id}")


@dataclass(frozen=True, slots=True)
class OpenUncertainty:
    """An UNKNOWN attempt without resolution, or an unresolved quarantine.

    ``amount`` is the attempt's submitted amount (None if its payload has none)
    or the quarantine's intended amount.
    """

    subject_kind: Literal["attempt", "quarantine"]
    subject_id: UUID
    symbol: str
    amount: Decimal | None


@dataclass(frozen=True, slots=True)
class ManagedOffer:
    """A live venue offer the ledger proves this scope placed (via one attempt)."""

    venue_offer_id: str
    symbol: str
    amount_remaining: Decimal
    attempt_id: UUID
    decision_id: str
    cell_id: str
    signal_correlation_id: str


@dataclass(frozen=True, slots=True)
class ManagedOffers:
    """Live managed offers, and live offers whose provenance contradicts itself.

    A conflicting offer is never guessed into ``offers`` nor treated as foreign.
    """

    offers: tuple[ManagedOffer, ...]
    provenance_conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CancelProvenance:
    venue_offer_id: str
    symbol: str
    attempt_id: UUID
    decision_id: str
    cell_id: str
    signal_correlation_id: str


class LedgerUncertainties(Protocol):
    async def open_uncertainties(
        self, session: AsyncSession, scope: Scope, symbol: str | None = None
    ) -> tuple[OpenUncertainty, ...]: ...


class LedgerManagedOffers(Protocol):
    """Reads over the offer mirror of the latest accepted snapshot and the journals.

    Every read runs on the caller's session; hold one REPEATABLE READ
    transaction across reads that must agree. Bounded by the live mirror rows
    and the attempts since the latest accepted basis, never by history.
    """

    async def managed_live_offers(
        self, session: AsyncSession, scope: Scope, symbols: Collection[str] | None = None
    ) -> ManagedOffers: ...

    async def cancel_provenance(
        self, session: AsyncSession, scope: Scope, venue_offer_id: str
    ) -> CancelProvenance | None:
        """The live managed offer's provenance; None when absent, terminal or foreign.

        Raises ``ProvenanceConflict`` on contradictory provenance.
        """
        ...

    async def fingerprints_in_use(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> frozenset[Decimal]:
        """Submitted amounts that still hold their fingerprint for ``symbol``."""
        ...


# ---------------------------------------------------------------------------
# Consumer read ports (S1-3c1). Runtime consumers depend on these Protocols
# only; apps injects an implementation (the legacy adapters today). Every
# method is bound to an explicit scope; a session argument joins the caller's
# transaction.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapitalAvailable:
    """A spendable capital answer for one symbol/cell.

    ``basis_token`` identifies the capital basis the answer was derived from.
    It is opaque: consumers compare tokens for equality or pass one back to the
    authority that issued it, and never parse it. User-visible output renders it
    with ``basis_token_value``.
    """

    applied: AppliedPolicy
    snapshot: CapitalSnapshot
    budget: CapitalBudget
    # Diagnostic: credits with no cell provenance. Part of total_capital, never
    # of snapshot.cell_exposure.
    unattributed_credit_exposure: Decimal
    basis_token: str
    attribution: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CapitalBlocked:
    """No capital answer: nothing may be spent. ``reason`` is the authority's code."""

    reason: str
    evidence: tuple[tuple[str, str], ...] = ()


type CapitalRead = CapitalAvailable | CapitalBlocked


def basis_token_value(token: str) -> int | str:
    """How a basis token appears in user-visible output (status, digests).

    A plain decimal token is rendered as the JSON number those outputs have
    always carried; any other token as its text. Presentation only: never use
    the result to decide anything.
    """
    if token.isascii() and token.isdigit() and (token == "0" or not token.startswith("0")):
        return int(token)
    return token


class CapitalAuthority(Protocol):
    async def read(
        self, scope: CapitalScope, *, now_ms: int, session: AsyncSession | None = None
    ) -> CapitalRead:
        """Capital for ``scope`` at ``now_ms`` (the caller's local clock).

        With ``session`` the read joins the caller's transaction; without one
        the authority reads in its own short, uncommitted session. Infrastructure
        failures raise; every authority refusal is a ``CapitalBlocked``.
        """
        ...

    async def read_policy(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> AppliedPolicy | CapitalBlocked:
        """The applied policy the scope's head points at, without taking any lock.

        Read-only guards and the planner's stop check use this; the command
        boundary re-binds the revision under the scope lock before any intent.
        """
        ...


@dataclass(frozen=True, slots=True)
class UncertaintyRecord:
    """One open uncertainty. ``kind`` is the authority's code; unknown kinds block."""

    exchange_account_id: UUID
    deployment_environment: str
    symbol: str
    kind: str


class UncertaintyReader(Protocol):
    """Open uncertainty for one exact account/environment (and symbol).

    ``session=None`` reads in the reader's own short session.
    """

    async def list_open(
        self, session: AsyncSession | None, scope: Scope, symbol: str | None = None
    ) -> tuple[UncertaintyRecord, ...]: ...

    async def has_open(self, session: AsyncSession | None, scope: Scope, symbol: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class LiveManagedOffer:
    """A live offer this scope placed (traced to an execution decision)."""

    venue_offer_id: str
    symbol: str
    signal_correlation_id: str | None


class ManagedOfferReader(Protocol):
    """Live-offer reads on the caller's session."""

    async def live(
        self, session: AsyncSession, scope: Scope, symbols: Collection[str] | None = None
    ) -> tuple[LiveManagedOffer, ...]:
        """Live managed offers (of ``symbols``, or all), ordered by venue offer id."""
        ...

    async def count_live(self, session: AsyncSession, scope: Scope, symbol: str) -> int:
        """How many live managed offers ``symbol`` has; manual offers never count."""
        ...

    async def live_symbols(self, session: AsyncSession, scope: Scope) -> frozenset[str]:
        """Symbols with any live offer, managed or not (the kill's cancel-all scope)."""
        ...

    async def fingerprints_in_use(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> frozenset[int]:
        """Amount fingerprints ``symbol``'s unresolved commitments still hold (D3a)."""
        ...


class ScopeLock(Protocol):
    async def lock(self, session: AsyncSession, scope: Scope) -> None:
        """Take the scope's transaction lock on ``session``; held until it ends."""
        ...


__all__ = [
    "CREDIT_STATUSES",
    "CREDIT_TERMINAL_KINDS",
    "OFFER_STATUSES",
    "OFFER_TERMINAL_KINDS",
    "Acceptance",
    "AcceptanceDecision",
    "Attempt",
    "CancelProvenance",
    "CapitalAuthority",
    "CapitalAvailable",
    "CapitalBlocked",
    "CapitalRead",
    "CapitalReadRefused",
    "Coverage",
    "Credit",
    "CreditHistory",
    "CreditKind",
    "CreditStatus",
    "CreditTerminalKind",
    "JsonObject",
    "LedgerCapitalRead",
    "LedgerCapitalReader",
    "LedgerJournal",
    "LedgerManagedOffers",
    "LedgerObservations",
    "LedgerReadUnbounded",
    "LedgerUncertainties",
    "LiveManagedOffer",
    "ManagedOffer",
    "ManagedOfferReader",
    "ManagedOffers",
    "Observation",
    "Offer",
    "OfferHistory",
    "OfferStatus",
    "OfferTerminalKind",
    "OpenUncertainty",
    "Outcome",
    "OutcomeAlreadyRecorded",
    "OutcomeKind",
    "ProvenanceConflict",
    "Quarantine",
    "QuarantineMember",
    "QuarantineMemberConflict",
    "QueryAdmissionRefused",
    "QueryHandle",
    "RecordedAttempt",
    "Resolution",
    "ResolutionAction",
    "ResolutionAlreadyRecorded",
    "ResolutionRejected",
    "Scope",
    "ScopeLock",
    "Trade",
    "UncertaintyReader",
    "UncertaintyRecord",
    "Wallet",
    "basis_token_value",
]
