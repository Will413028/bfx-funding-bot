"""Dormant, transaction-scoped ledger journal, observation, and read contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger.conservation import (
    LEDGER_EPSILON,
    AcceptedConservation,
    Conservation,
    ConservationVerdict,
    OfferFill,
    SymbolConservation,
    conservation_verdict,
)
from bfx_funding_bot.modules.ledger.matching import (
    UNKNOWN_SETTLE_MS,
    AutoAction,
    AutoDecision,
    MatchEvidence,
    UnknownMatch,
    UnknownMatchKind,
    UnknownTerms,
    amount_seen_since_start,
    decide_unknown,
    match_unknown,
)
from bfx_funding_bot.modules.trading import (
    AppliedPolicy,
    CapitalBudget,
    CapitalPolicy,
    CapitalResult,
    CapitalScope,
    CapitalSnapshot,
)

# How long after it started an attempt (or a venue offer without provenance) may still be in
# flight: a submit's transport may be sent before its outcome is journaled. At boot the
# process holds the writer lock and nothing is in flight, so nothing needs to be waited for.
BOOT_GRACE_MS = 0
RUNTIME_GRACE_MS = 120_000

type JsonObject = dict[str, object]
type OutcomeKind = Literal["ack", "rejected", "not_sent", "unknown"]
type ResolutionAction = Literal["bound_to_venue", "not_accepted", "manual"]
type CreditKind = Literal["credit", "loan"]
type AcceptanceDecision = Literal["accepted", "fenced", "incomplete_or_unequal"]

# Closed status vocabularies. The port normalizes Bitfinex strings into these;
# ledger never parses venue text.
#   offer status       "ACTIVE" -> active; "PARTIALLY FILLED ..." -> partially_filled
#   offer terminal     "EXECUTED ..." -> executed; "CANCELED ..." -> canceled;
#                      offer "EXPIRED" -> canceled (ended unfilled)
#   credit/loan status "ACTIVE" -> active
#   credit terminal    "CLOSED" and every "CLOSED (<reason>)" -> closed
#   history row status "was: PARTIALLY FILLED" -> partially_filled, otherwise active;
#                      occurred_at_ms = mts_update
# Unknown status: active stream raises (no observation); history stream is
# incomplete and alerts. These are normalizer rules, not ledger text parsing.
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
    # The venue payload; empty when the offer was loaded for matching, which never reads it.
    raw: JsonObject = field(default_factory=dict)


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
    """What the port's reads cover. Two kinds of offer-history evidence, with two contracts:

    * the windowed query (``history_requested_*``, ``offer_history_pages``,
      ``history_oldest/newest_mts_created``): offers that CHANGED in the requested range
      (the venue filters offers by MTS_UPDATE), paged to exhaustion. Absence from it proves
      something only inside that range; the UNKNOWN matcher and R6 read only this.
    * point lookups by id of the offers that vanished from the active list. Their rows are
      terminal evidence for exactly those ids and never widen the range, the page counts
      or the oldest/newest stamps above; absence of an id is not evidence (see
      ``Observation.unconfirmed_ends``).

    ``offer_history_complete`` is true when the windowed query was exhausted and every
    vanished id was either found by id or declared in ``Observation.unconfirmed_ends``.
    """

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
    # The symbols whose history streams the port actually fetched. Per-symbol
    # absence proofs (UNKNOWN matching, R6) hold only for these; None = the
    # port declared nothing, which fails closed.
    history_symbols: frozenset[str] | None = None

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
    # Ids of offers that left the active list and that the venue still does not know
    # by id after the port's grace. No terminal row exists for them and none is made:
    # absence never confirms terminal. Conservation determines their fill from this
    # observation's funding trades (by OFFER_ID) or, if it cannot, conflicts.
    unconfirmed_ends: tuple[str, ...] = ()


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
class CycleResult:
    """Cycle disposition; venue/legacy accounting details stay adapter-specific."""

    decision: Literal["accepted", "query_admission_refused", "fenced", "incomplete_or_unequal"]
    observation_id: UUID | None = None
    # Automatic UNKNOWN resolutions committed with this cycle (for post-commit notices).
    resolutions: tuple[UUID, ...] = ()


class ObservationSink(Protocol):
    async def run(self, scope: Scope) -> CycleResult: ...


@dataclass(frozen=True, slots=True)
class OfferCloseHint:
    """Untrusted venue facts, never reservation authority or CID correlation."""

    venue_offer_id: str
    symbol: str
    kind: str  # original status text; legacy keeps its substring precedence
    rate: float
    occurred_at_ms: int
    venue_seq: int | None
    received_at_ms: int
    cancel_requested_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CreditCloseHint:
    credit_id: int
    symbol: str
    amount: Decimal
    rate: float
    period_days: int
    mts_create: int
    occurred_at_ms: int
    venue_seq: int | None
    mts_opening: int | None = None
    mts_last_payout: int | None = None


class VenueHintSink(Protocol):
    """Scope is bound at construction, like the legacy registry/account.

    Legacy persists authority before publishing. Ledger requests reconciliation
    and publishes only non-authoritative notifications, without any DB writes.
    offer_gone returns False only when the caller should retry next poll.
    """

    async def offer_closed(self, hint: OfferCloseHint) -> None: ...

    async def credit_closed(self, hint: CreditCloseHint) -> None: ...

    async def offer_gone(self, venue_offer_id: str, *, occurred_at_ms: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class VenueHintNotification:
    """Non-authoritative wake-up evidence; never consumed as capital truth.

    Credit-close frames have no offer linkage: credit_id is their only venue
    key. No new provenance read is introduced here, so attempt_id is omitted.
    """

    scope: Scope
    kind: Literal["offer_closed", "credit_closed", "offer_gone"]
    occurred_at_ms: int
    venue_seq: int | None = None
    venue_offer_id: str | None = None
    credit_id: int | None = None
    status: str | None = None


class VenueHintPublisher(Protocol):
    async def publish(self, event: VenueHintNotification) -> None: ...


@dataclass(frozen=True, slots=True)
class UnknownResolutionNotice:
    """An UNKNOWN submit the system resolved by itself in an accepted cycle.

    Published only after that cycle's transaction committed; non-authoritative.
    """

    scope: Scope
    observation_id: UUID
    resolution_id: UUID


@dataclass(frozen=True, slots=True)
class QueryHandle:
    query_id: UUID
    query_revision: int
    start_revision: int
    started_at_ms: int


@dataclass(frozen=True, slots=True)
class LiveOffer:
    """An offer in the latest accepted snapshot: what the port needs to look its end up by id."""

    venue_offer_id: str
    symbol: str


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    """Local anchors for the next observation's history request.

    The attempt anchor covers the high-water tail, basis-unresolved attempts
    and open UNKNOWNs, plus source attempts of unresolved R6 quarantines.
    history_start_ms is the earlier anchor minus the history margin, or None
    when neither exists (the very first observation).
    Without an accepted basis, use the earliest attempt; with no attempts
    there is no history anchor. The caller owns that first-observation case.
    """

    earliest_attempt_started_at_ms: int | None
    previous_query_started_at_ms: int | None
    history_start_ms: int | None
    # Symbols of the anchor attempts: the port must fetch their history even
    # when no wallet or credit names them.
    anchor_symbols: frozenset[str] = frozenset()
    # The previous accepted snapshot's live offers. Conservation needs the end of
    # every one that is gone from the next active read, however old it is; the
    # anchor window above cannot certify that. The port computes the vanished set
    # and asks the venue for those offers by id.
    live_offers: tuple[LiveOffer, ...] = ()


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
class Authorized(RecordedAttempt):
    """The CAS passed and the attempt was journaled in the caller's transaction."""


@dataclass(frozen=True, slots=True)
class AuthorizeRefused:
    reason: Literal["capital_snapshot_changed", "capital_policy_revision_changed", "query_pending"]


OBSERVATION_REF_PREFIX = "ledger:v1:obs:"


def observation_evidence_ref(observation_id: UUID) -> str:
    """The opaque evidence reference naming one ledger observation."""
    return f"{OBSERVATION_REF_PREFIX}{observation_id}"


def encode_basis_token(query_id: UUID, clock_revision: int) -> str:
    """Ledger-only token; consumers carry it opaquely back to authorization."""
    if clock_revision < 0:
        raise ValueError("clock revision must be nonnegative")
    return f"ledger:v1:{query_id}:{clock_revision}"


def parse_basis_token(token: str) -> tuple[UUID, int]:
    """Reject malformed, noncanonical and legacy tokens."""
    parts = token.split(":")
    if len(parts) != 4 or parts[:2] != ["ledger", "v1"]:
        raise ValueError("invalid ledger basis token")
    query_id, revision = UUID(parts[2]), int(parts[3])
    if encode_basis_token(query_id, revision) != token:
        raise ValueError("invalid ledger basis token")
    return query_id, revision


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
    source_attempt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class QuarantineMember:
    quarantine_id: UUID
    source_kind: Literal["offer", "credit", "loan"]
    venue_object_id: str
    observation_id: UUID
    amount_at_join: Decimal


# ---------------------------------------------------------------------------
# Legacy closure seed (S1-4d, ADR D6'/D7'''). A neutral closure the legacy reader in
# execution produces from the final legacy accepted snapshot, and ``ledger.seed.write_seed``
# turns into the one ``legacy_seed`` observation, its basis, journals, mirrors and quarantines.
# Every value here is a legacy fact as stored (F7: no re-derivation); vocabularies are the
# ledger's, mapped by the reader. Deleted with the legacy authority in S1-8 (the DTOs and
# ``write_seed`` stay as the record of how the seed rows were made).
# ---------------------------------------------------------------------------

type SeedClassification = Literal["reflected", "settled", "unresolved"]
type AttributionBasis = Literal["trade", "carry", "recent_fill", "unattributed"]
SEED_CLASSIFICATIONS: frozenset[str] = frozenset(("reflected", "settled", "unresolved"))
ATTRIBUTION_BASES: frozenset[str] = frozenset(("trade", "carry", "recent_fill", "unattributed"))


@dataclass(frozen=True, slots=True)
class SeedOffer:
    """A live venue offer of the final legacy snapshot (ours and foreign alike)."""

    venue_offer_id: str
    symbol: str
    amount_original: Decimal
    amount_remaining: Decimal
    rate: Decimal | None
    period_days: int | None
    offer_type: str | None
    flags: JsonObject | None
    status: OfferStatus
    mts_created: int
    mts_updated: int | None


@dataclass(frozen=True, slots=True)
class SeedCredit:
    """A live credit or loan of the final legacy snapshot; ``mts_opening`` is required."""

    source_kind: CreditKind
    venue_credit_id: str
    symbol: str
    amount: Decimal
    rate: Decimal | None
    period_days: int
    status: CreditStatus
    flags: JsonObject | None
    mts_created: int | None
    mts_updated: int | None
    mts_opening: int


@dataclass(frozen=True, slots=True)
class SeedSymbol:
    """One symbol's legacy accepted totals and per-cell exposure (offers + credits)."""

    symbol: str
    available: Decimal
    offered: Decimal
    credits: Decimal
    unattributed_credits: Decimal
    foreign_offers: Decimal
    cells: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class SeedCreditGroup:
    """The legacy attribution of one live credit, keyed like the ledger's carry."""

    source_kind: CreditKind
    venue_credit_id: str
    symbol: str
    amount: Decimal
    period_days: int
    mts_opening: int
    attribution_basis: AttributionBasis
    cells: frozenset[str]


@dataclass(frozen=True, slots=True)
class SeedOutcome:
    kind: OutcomeKind
    venue_offer_id: str | None
    reason: str | None
    completed_at_ms: int
    evidence: JsonObject


@dataclass(frozen=True, slots=True)
class SeedAttempt:
    """A legacy submission attempt (or the synthesized partial of a claim-only live offer).

    ``attempt_seq`` is the legacy intent's ``event_seq`` (per-scope monotonic, never
    synthetic); ``provenance`` names the legacy rows (attempt, decision, claim, events,
    uncertainty) and becomes ``seed_provenance``.
    """

    attempt_id: UUID
    execution_decision_id: str
    symbol: str
    cell_id: str
    attempt_seq: int
    normalized_payload: JsonObject
    started_at_ms: int
    outcome: SeedOutcome
    classification: SeedClassification
    provenance: JsonObject


@dataclass(frozen=True, slots=True)
class SeedQuarantineMember:
    source_kind: Literal["offer", "credit", "loan"]
    venue_object_id: str
    amount_at_join: Decimal


@dataclass(frozen=True, slots=True)
class SeedQuarantine:
    """An open legacy quarantine-kind uncertainty; ``quarantine_id`` is its uncertainty id."""

    quarantine_id: UUID
    symbol: str
    intended_amount: Decimal
    opened_at_ms: int
    evidence: JsonObject
    members: tuple[SeedQuarantineMember, ...] = ()


@dataclass(frozen=True, slots=True)
class SeedWatermarks:
    """Where the capture point is in legacy terms (evidence and anti-join anchors)."""

    final_event_seq: int
    snapshot_event_seq: int
    snapshot_query_id: UUID
    snapshot_command_fence: int
    trading_state_max_id: int | None


@dataclass(frozen=True, slots=True)
class SeedClosure:
    """Everything the seed writes for one scope, read in one snapshot of the legacy tables."""

    scope: Scope
    watermarks: SeedWatermarks
    query_started_at_ms: int
    query_finished_at_ms: int
    confirmation_finished_at_ms: int
    offers: tuple[SeedOffer, ...]
    credits: tuple[SeedCredit, ...]
    symbols: tuple[SeedSymbol, ...]
    credit_groups: tuple[SeedCreditGroup, ...]
    attempts: tuple[SeedAttempt, ...]
    quarantines: tuple[SeedQuarantine, ...]
    evidence: JsonObject


class SeedRefused(ValueError):  # noqa: N818 - a refusal, named by the seed contract
    """The closure cannot be seeded truthfully; ``reason`` is a controlled code."""

    def __init__(self, reason: str, *detail: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class OutcomeAlreadyRecorded(ValueError):  # noqa: N818 - named by journal contract
    def __init__(self, stored: Outcome | CommandOutcome) -> None:
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

    def __init__(self, code: str, *, kind: Literal["conflict", "invalid", "not_found"] = "conflict") -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ResolutionSubject:
    """Storage-neutral subject identity; venue correlation belongs to adapters."""

    uncertainty_id: UUID
    symbol: str
    attempt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ResolutionEvidence:
    evidence_ref: str | None = None
    query_started_at_ms: int | None = None
    query_finished_at_ms: int | None = None
    candidate_venue_offer_ids: tuple[str, ...] = ()
    candidate_count: int | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedEvidence:
    evidence_ref: str
    query_started_at_ms: int
    query_finished_at_ms: int
    candidate_venue_offer_ids: tuple[str, ...] = ()
    candidate_count: int | None = None
    match_kind: str | None = None
    venue_offer_id: str | None = None
    venue_status: str | None = None


class OperatorEvidence(Protocol):
    async def resolution_context(
        self, session: AsyncSession, scope: Scope, subject: ResolutionSubject,
    ) -> ResolutionEvidence: ...

    async def verify(
        self, session: AsyncSession, scope: Scope, subject: ResolutionSubject,
        evidence_ref: str, *, require_history: bool,
    ) -> VerifiedEvidence:
        """Re-derive the fence or raise ResolutionRejected with a bounded code."""
        ...


type OperatorAction = Literal["bind_to_venue", "mark_not_accepted", "manual_resolution"]
# The decisions an operator may state for a manual resolution (a quarantine only).
MANUAL_RESOLUTION_DECISIONS: frozenset[str] = frozenset(
    {"accepted_external_exposure", "closed_at_venue"}
)


@dataclass(frozen=True, slots=True)
class ResolutionIntent:
    """What the operator asked for; ``evidence_ref`` is opaque until the authority maps it."""

    uncertainty_id: UUID
    action: OperatorAction
    evidence_ref: str
    operator_id: str
    reason: str | None = None
    venue_offer_id: str | None = None
    decision: str | None = None


@dataclass(frozen=True, slots=True)
class RequestColumns:
    """The authority's own evidence column of a request row: exactly one is set."""

    reconcile_event_seq: int | None = None
    observation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class QueuedResolution:
    """A request row as its authority applies it."""

    request_id: UUID
    intent: ResolutionIntent
    columns: RequestColumns


@dataclass(frozen=True, slots=True)
class AppliedResolution:
    """What applying produced; ``resolved_event_seq`` exists only under the legacy log."""

    resolved_event_seq: int | None = None


class OperatorResolution(Protocol):
    """The operator request path of one authority: validated at the web API, applied by the daemon.

    Both methods raise ``ResolutionRejected`` with the bounded operator codes.
    """

    def columns(self, evidence_ref: str) -> RequestColumns:
        """Map a well-formed reference to its request column, else ``stale_reconcile_fence``."""
        ...

    async def prepare(
        self, session: AsyncSession, scope: Scope, intent: ResolutionIntent, *, now_ms: int
    ) -> RequestColumns:
        """Everything the web API's own role can prove before queueing; returns the columns."""
        ...

    async def apply(
        self, session: AsyncSession, scope: Scope, request: QueuedResolution, *, now_ms: int
    ) -> AppliedResolution:
        """Authoritative re-validation and the write, in the worker's locked transaction."""
        ...


class QueryAdmissionRefused(ValueError):  # noqa: N818 - named by query contract
    """An in-flight command makes a pre-I/O fence unsafe."""


class LedgerJournal(Protocol):
    async def bump_clock(self, session: AsyncSession, scope: Scope) -> int: ...
    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle: ...
    async def authorize_attempt(
        self,
        session: AsyncSession,
        scope: Scope,
        attempt: Attempt,
        basis_token: str,
        *,
        now_ms: int,
    ) -> Authorized | AuthorizeRefused: ...
    async def close_dangling(
        self, session: AsyncSession, scope: Scope, *, now_ms: int, grace_ms: int
    ) -> tuple[UUID, ...]: ...
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
        self, session: AsyncSession, scope: Scope, opening: Quarantine
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
    query_id: UUID | None
    clock_revision: int | None


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


class VenueObservation(Protocol):
    """Read venue evidence after the caller has committed ``begin_query``.

    Returns exactly the evidence arguments to ``LedgerObservations.accept``:
    first read, active-only confirmation, and confirmation start time. No DB
    session or acceptance decision crosses this I/O boundary.
    """

    async def observe(
        self, scope: Scope, query_started_at_ms: int, window: ObservationWindow
    ) -> tuple[Observation, Observation, int]: ...


class LedgerObservations(Protocol):
    """Commit a query fence, then accept its two reads (an accepted one writes the basis).

    ``begin_query`` runs in its own committed transaction before any venue I/O;
    ``accept`` runs in a later transaction on the caller's session.
    """

    async def begin_query(
        self, session: AsyncSession, scope: Scope, started_at_ms: int
    ) -> QueryHandle: ...
    async def observation_window(
        self, session: AsyncSession, scope: Scope
    ) -> ObservationWindow: ...
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
    # The venue terms of the mirror row (what a reprice reads); ``rate`` and
    # ``period_days`` are None when the venue did not report them.
    rate: Decimal | None
    rate_observed: bool
    period_days: int | None
    mts_created: int
    amount_original: Decimal | None
    status: str


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
    attempt_id: UUID | None
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
        """Live managed or ack-only provenance; None when missing, terminal or foreign.

        Raises ``ProvenanceConflict`` on contradictory provenance.
        """
        ...

    async def fingerprints_in_use(
        self, session: AsyncSession, scope: Scope, symbol: str
    ) -> frozenset[Decimal]:
        """Submitted amounts that still hold their fingerprint for ``symbol``."""
        ...


class LedgerConservationReader(Protocol):
    """The conservation verdicts stored with the scope's latest accepted basis.

    Acceptance computes and stores them; this only reads. ``None`` when the scope
    has no accepted basis. Run it on the caller's session.
    """

    async def latest(self, session: AsyncSession, scope: Scope) -> AcceptedConservation | None: ...


@dataclass(frozen=True, slots=True)
class AcceptedSymbolPosition:
    """One symbol of the latest accepted basis, as a venue snapshot's per-symbol totals.

    ``offered`` is the managed offers' remaining amount; ``foreign_offers`` the live offers
    nothing of this scope placed (kept out of ``offered``), so the symbol's reserved total
    is their sum. ``n_offers`` counts every live offer of the symbol.
    """

    symbol: str
    available: Decimal
    offered: Decimal
    foreign_offers: Decimal
    credits: Decimal
    n_credits: int
    n_offers: int


@dataclass(frozen=True, slots=True)
class AcceptedPositions:
    observation_id: UUID
    accepted_at_ms: int
    symbols: tuple[AcceptedSymbolPosition, ...]


@dataclass(frozen=True, slots=True)
class ForeignOffer:
    """A live venue offer no attempt of this scope accounts for."""

    venue_offer_id: str
    symbol: str
    amount_remaining: Decimal
    amount_original: Decimal | None
    # None when the venue did not report them (``rate_observed`` is then False).
    rate: Decimal | None
    rate_observed: bool
    period_days: int | None
    mts_created: int
    status: str


@dataclass(frozen=True, slots=True)
class ForeignOffers:
    """The foreign live offers, and the id of every live offer (managed or not).

    An offer that is the candidate of an open UNKNOWN submit may be ours, and an offer with
    contradictory provenance is neither: neither is listed in ``offers``.
    """

    offers: tuple[ForeignOffer, ...]
    live_offer_ids: frozenset[str]


class LedgerCycleReads(Protocol):
    """What the effects of an accepted cycle read beyond the capital and operator reads.

    Both run on the caller's session over the scope's latest accepted basis and its offer
    mirror; ``accepted_positions`` is None when the scope has no accepted basis.
    """

    async def accepted_positions(
        self, session: AsyncSession, scope: Scope
    ) -> AcceptedPositions | None: ...

    async def foreign_live_offers(self, session: AsyncSession, scope: Scope) -> ForeignOffers: ...


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
    authority that issued it, and never parse it. User-visible output preserves
    the string unchanged.
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
class UncertaintyView:
    """One operator-visible uncertainty, open or resolved, in the authority's own terms.

    ``kind`` is the authority's code; ``evidence`` is its raw evidence mapping, which
    the API boundary filters before any caller sees it. ``attempt_id`` names the
    submission attempt of an attempt-sourced uncertainty (None for a quarantine).
    """

    uncertainty_id: UUID
    kind: str
    symbol: str
    state: str
    intended_amount: Decimal
    evidence: Mapping[str, object]
    attempt_id: UUID | None = None
    opened_at_ms: int | None = None
    resolved_at_ms: int | None = None
    resolved_by_operator_id: str | None = None
    resolution_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PositionView:
    """One symbol's capital split into disjoint components, in the symbol's own units.

    ``available + offered + lent`` is the symbol's total. ``unattributed_lent`` is a
    SUBSET of ``lent`` (credits no managed attempt owns), never an addend; None when
    the authority has no such fact.
    """

    symbol: str
    available: Decimal
    offered: Decimal
    lent: Decimal
    unattributed_lent: Decimal | None
    n_credits: int | None
    last_updated_ms: int
    last_reconciled_at_ms: int | None


@dataclass(frozen=True, slots=True)
class OfferView:
    """One managed offer. ``offer_key`` is opaque and stable for the offer's life."""

    offer_key: str
    venue_offer_id: str | None
    state: str
    symbol: str
    size_usdt: Decimal
    occurred_at_ms: int
    last_updated_ms: int


class OperatorReads(Protocol):
    """Operator-console reads of one account scope on the caller's session.

    The authority epoch picks the implementation at boot; callers never learn which.
    """

    async def list_uncertainties(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        state: Literal["open", "resolved"] | None,
        limit: int,
    ) -> tuple[UncertaintyView, ...]:
        """Newest first (open by opening, resolved by resolution), at most ``limit``."""
        ...

    async def get_uncertainty(
        self, session: AsyncSession, scope: Scope, uncertainty_id: UUID
    ) -> UncertaintyView | None:
        """None when absent or of another scope (the two are indistinguishable)."""
        ...

    async def list_positions(
        self, session: AsyncSession, scope: Scope
    ) -> tuple[PositionView, ...]:
        """One view per symbol, ordered by symbol."""
        ...

    async def list_offers(
        self, session: AsyncSession, scope: Scope, *, states: Collection[str]
    ) -> tuple[OfferView, ...]:
        """Offers whose state is in ``states``, newest update first."""
        ...


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


class PolicyRefused(ValueError):  # noqa: N818 - a refusal, carrying the store's reason code
    """The policy store refused to read or write; ``str(exc)`` is its reason code."""


class PolicyStore(Protocol):
    """The scope's applied capital policy: the shared policy heads and revisions.

    Both authorities keep these two tables and share the one writer
    (``ledger.policy_write``); what differs is what a store does first (the legacy one
    replays its event stream). Lock ownership: the caller holds the scope lock
    (``ScopeLock.lock``) across ``read_applied`` and the ``apply_policy`` based on it; the
    store never takes it for the caller. The caller owns the transaction.
    """

    @property
    def scope(self) -> Scope: ...

    async def read_applied(self, session: AsyncSession, *, symbol: str) -> AppliedPolicy:
        """The revision ``symbol``'s head points at, proven and parsed, or ``PolicyRefused``."""
        ...

    async def apply_policy(
        self, session: AsyncSession, *, symbol: str, policy: CapitalPolicy,
        expected_revision: int, source: dict[str, Any],
    ) -> AppliedPolicy:
        """Append revision ``expected_revision + 1`` and move the head; the caller commits.

        ``PolicyRefused`` when the head moved (``revision_changed``) or the symbol may
        not be enabled (``unsupported_enabled_symbol``).
        """
        ...


@dataclass(frozen=True, slots=True)
class CommandAttempt:
    """Consumer submit identity; legacy CID belongs exclusively to its adapter."""

    attempt_id: UUID
    execution_decision_id: str
    symbol: str
    normalized_payload: JsonObject
    amount: Decimal
    started_at_ms: int
    policy_revision: int
    policy_digest: str
    policy_revision_id: UUID
    command_date: date | None = None  # freeze the legacy identity day across midnight
    event_id: UUID | None = None
    cell_id: str | None = None


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    kind: OutcomeKind
    venue_offer_id: str | None
    reason: str | None
    completed_at_ms: int
    evidence: JsonObject
    event_id: UUID | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class CommandRefused:
    reason: str


@dataclass(frozen=True, slots=True)
class CancelAdmitted:
    provenance: CancelProvenance
    amount: Decimal
    rate: Decimal
    period_days: int


type LockedCommandGuard = Callable[[AsyncSession], Awaitable[None]]
type LockedCancelGuard = Callable[[AsyncSession, CancelAdmitted], Awaitable[None]]


class CommandJournal(Protocol):
    """Admission on the caller's txn; outcomes on an owned short transaction.

    The port locks the scope, validates, runs the guard, then writes. Transport
    starts only after the caller has committed. No CID crosses this boundary.
    """

    async def authorize(
        self, session: AsyncSession, scope: Scope, attempt: CommandAttempt,
        basis_token: str, *, now_ms: int, locked_guard: LockedCommandGuard,
    ) -> Authorized | CommandRefused: ...

    async def record_outcome(
        self, scope: Scope, attempt_id: UUID, outcome: CommandOutcome,
    ) -> None: ...

    async def read_back_outcome(
        self, scope: Scope, attempt_id: UUID,
    ) -> CommandOutcome | None: ...

    async def admit_cancel(
        self, session: AsyncSession, scope: Scope, venue_offer_id: str,
        *, now_ms: int, locked_guard: LockedCancelGuard,
    ) -> CancelAdmitted | CommandRefused: ...


__all__ = [
    "ATTRIBUTION_BASES",
    "BOOT_GRACE_MS",
    "CREDIT_STATUSES",
    "CREDIT_TERMINAL_KINDS",
    "LEDGER_EPSILON",
    "MANUAL_RESOLUTION_DECISIONS",
    "OBSERVATION_REF_PREFIX",
    "OFFER_STATUSES",
    "OFFER_TERMINAL_KINDS",
    "RUNTIME_GRACE_MS",
    "SEED_CLASSIFICATIONS",
    "UNKNOWN_SETTLE_MS",
    "Acceptance",
    "AcceptanceDecision",
    "AcceptedConservation",
    "AcceptedPositions",
    "AcceptedSymbolPosition",
    "AppliedResolution",
    "Attempt",
    "AttributionBasis",
    "AuthorizeRefused",
    "Authorized",
    "AutoAction",
    "AutoDecision",
    "CancelAdmitted",
    "CancelProvenance",
    "CapitalAuthority",
    "CapitalAvailable",
    "CapitalBlocked",
    "CapitalRead",
    "CapitalReadRefused",
    "CommandAttempt",
    "CommandJournal",
    "CommandOutcome",
    "CommandRefused",
    "Conservation",
    "ConservationVerdict",
    "Coverage",
    "Credit",
    "CreditCloseHint",
    "CreditHistory",
    "CreditKind",
    "CreditStatus",
    "CreditTerminalKind",
    "CycleResult",
    "ForeignOffer",
    "ForeignOffers",
    "JsonObject",
    "LedgerCapitalRead",
    "LedgerCapitalReader",
    "LedgerConservationReader",
    "LedgerCycleReads",
    "LedgerJournal",
    "LedgerManagedOffers",
    "LedgerObservations",
    "LedgerReadUnbounded",
    "LedgerUncertainties",
    "LiveManagedOffer",
    "LiveOffer",
    "LockedCancelGuard",
    "LockedCommandGuard",
    "ManagedOffer",
    "ManagedOfferReader",
    "ManagedOffers",
    "MatchEvidence",
    "Observation",
    "ObservationSink",
    "ObservationWindow",
    "Offer",
    "OfferCloseHint",
    "OfferFill",
    "OfferHistory",
    "OfferStatus",
    "OfferTerminalKind",
    "OfferView",
    "OpenUncertainty",
    "OperatorAction",
    "OperatorEvidence",
    "OperatorReads",
    "OperatorResolution",
    "Outcome",
    "OutcomeAlreadyRecorded",
    "OutcomeKind",
    "PolicyRefused",
    "PolicyStore",
    "PositionView",
    "ProvenanceConflict",
    "Quarantine",
    "QuarantineMember",
    "QuarantineMemberConflict",
    "QueryAdmissionRefused",
    "QueryHandle",
    "QueuedResolution",
    "RecordedAttempt",
    "RequestColumns",
    "Resolution",
    "ResolutionAction",
    "ResolutionAlreadyRecorded",
    "ResolutionEvidence",
    "ResolutionIntent",
    "ResolutionRejected",
    "ResolutionSubject",
    "Scope",
    "ScopeLock",
    "SeedAttempt",
    "SeedClassification",
    "SeedClosure",
    "SeedCredit",
    "SeedCreditGroup",
    "SeedOffer",
    "SeedOutcome",
    "SeedQuarantine",
    "SeedQuarantineMember",
    "SeedRefused",
    "SeedSymbol",
    "SeedWatermarks",
    "SymbolConservation",
    "Trade",
    "UncertaintyReader",
    "UncertaintyRecord",
    "UncertaintyView",
    "UnknownMatch",
    "UnknownMatchKind",
    "UnknownResolutionNotice",
    "UnknownTerms",
    "VenueHintNotification",
    "VenueHintPublisher",
    "VenueHintSink",
    "VenueObservation",
    "VerifiedEvidence",
    "Wallet",
    "amount_seen_since_start",
    "conservation_verdict",
    "decide_unknown",
    "encode_basis_token",
    "match_unknown",
    "observation_evidence_ref",
    "parse_basis_token",
]
