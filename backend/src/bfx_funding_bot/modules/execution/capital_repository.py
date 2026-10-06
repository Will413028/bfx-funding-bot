"""Transaction-scoped capital authority. This is not permission to call a venue.

Every public operation locks/replays the account stream. The caller owns the
transaction and the live ownership/halt/eligibility guard. In particular halt
writers MUST take the same account/environment xact lock (Task3). Commit before
any transport; never retry a committed intent after a crash.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, NamedTuple, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_policy_read import (
    CapitalBlockedError,
    policy_from_row,
    read_policy_row,
)
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalQueryRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.event_store.entities import (
    VenueCreditObservation,
    is_terminal_offer_status,
)
from bfx_funding_bot.modules.execution.event_store.historical_claims import (
    historical_claim_reset_sequences,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    deserialize_stored_event,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore, SnapshotDrift
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    EventPrefixHashRow,
    OfferClaimRow,
)
from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter
from bfx_funding_bot.modules.execution.events import (
    ReservationIntent,
    UncertaintyMarkedNotAccepted,
    VenueSnapshotObserved,
)
from bfx_funding_bot.modules.execution.submit_outcomes import SubmissionAttemptPayload
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    ExecutionUncertaintyRow,
    SubmissionAttemptRow,
)
from bfx_funding_bot.modules.ledger import PolicyRefused, Scope
from bfx_funding_bot.modules.ledger.policy_write import write_policy_revision
from bfx_funding_bot.modules.live_validation.tables import FundingTradeRow
from bfx_funding_bot.modules.trading import (
    CapitalBudget,
    CapitalPolicy,
    CapitalSnapshot,
    evaluate_capital,
    policy_digest,
    policy_payload,
)

ZERO = Decimal("0")
SCHEMA_VERSION = 1


class _SnapshotBasis(NamedTuple):
    """The validated accepted classification, before any commitment fold.

    Built from a stored snapshot row (``_snapshot_basis``) or from an observation
    evaluated without writing (``read_observed``); both go through
    ``_classified_basis``.
    """

    classification: Mapping[str, Any]
    command_fence: int
    event: VenueSnapshotObserved
    available: Decimal
    offered: Decimal
    credits: Decimal
    shared: Decimal
    exposure: Decimal
LockedGuard = Callable[[AsyncSession], Awaitable[None]]


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _amount(value: object) -> Decimal:
    try:
        if not isinstance(value, (str, Decimal)):
            raise ValueError
        result = Decimal(value)
        if not result.is_finite() or result < ZERO:
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CapitalBlockedError("invalid_capital_amount") from exc


@dataclass(frozen=True, slots=True)
class AppliedCapitalPolicy:
    revision: int
    digest: str
    policy: CapitalPolicy
    revision_id: UUID


@dataclass(frozen=True, slots=True)
class CapitalView:
    applied: AppliedCapitalPolicy
    snapshot_seq: int
    snapshot: CapitalSnapshot
    budget: CapitalBudget
    # Diagnostic: credits with no cell provenance (U). Part of total_capital,
    # never of snapshot.cell_exposure.
    unattributed_credit_exposure: Decimal
    attribution: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ObservedAcceptance:
    """An observation evaluated as ``accept_snapshot`` would, nothing stored (F3 (i'))."""

    event: VenueSnapshotObserved
    command_fence: int
    classification: Mapping[str, Any]
    # What acceptance would store as ``authorization_blocked_reason``.
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class ObservedCapital:
    """``CapitalView`` of an ``ObservedAcceptance``: no snapshot row, so no ``snapshot_seq``."""

    applied: AppliedCapitalPolicy
    command_fence: int
    snapshot: CapitalSnapshot
    budget: CapitalBudget
    unattributed_credit_exposure: Decimal
    attribution: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AuthorizedIntent:
    event_seq: int
    intent: ReservationIntent
    capital: CapitalView


class CapitalRepository:
    def __init__(self, *, account_id: UUID, environment: str,
                 max_snapshot_age_ms: int) -> None:
        if not isinstance(account_id, UUID) or not environment.strip():
            raise ValueError("capital requires canonical account/environment")
        if type(max_snapshot_age_ms) is not int or max_snapshot_age_ms <= 0:
            raise ValueError("max_snapshot_age_ms must be positive")
        self.account_id = account_id
        self.environment = environment
        self.max_snapshot_age_ms = max_snapshot_age_ms
        self._store = PostgresEventStore(deployment_environment=environment)
        self.writer = AccountEventWriter(store=self._store)

    def _scope(self, table: Any) -> tuple[Any, Any]:
        return (table.exchange_account_id == self.account_id,
                table.deployment_environment == self.environment)

    async def _prepare(self, session: AsyncSession) -> None:
        await self.writer.prepare_locked(session, account_id=self.account_id)

    async def apply_policy(self, session: AsyncSession, *, symbol: str, policy: CapitalPolicy,
                           expected_revision: int, source: dict[str, Any]) -> AppliedCapitalPolicy:
        await self._prepare(session)
        try:
            written = await write_policy_revision(
                session, Scope(self.account_id, self.environment), symbol=symbol,
                policy=policy, expected_revision=expected_revision, source=source,
            )
        except PolicyRefused as exc:
            raise CapitalBlockedError(str(exc)) from exc
        return AppliedCapitalPolicy(written.revision, written.digest, policy, written.revision_id)

    async def read_applied(self, session: AsyncSession, *, symbol: str) -> AppliedCapitalPolicy:
        await self._prepare(session)
        row = await read_policy_row(session, account_id=self.account_id,
                                    environment=self.environment, symbol=symbol)
        policy = policy_from_row(row)
        return AppliedCapitalPolicy(row.revision, row.digest, policy, row.id)

    async def _fence(self, session: AsyncSession) -> int:
        # Conservative stream fence includes cancel, outcomes, WS and reconciles.
        return int(await session.scalar(select(func.max(EventLogRow.event_seq)).where(
            *self._scope(EventLogRow))) or 0)

    async def _assert_no_unknown(self, session: AsyncSession, *, symbol: str) -> None:
        """Refuse ``symbol`` while it has open uncertainty (ladder level 2).

        Per symbol, not per account: each symbol's capital is its own wallet, so
        an UNKNOWN on one says nothing about another's cash. What it must never
        do is let its own possible commitment read as available -- the read
        refuses the symbol here, and again from the accepted snapshot's
        ``unresolved`` set until a snapshot taken after the resolution replaces it.

        One indexed probe, nothing per attempt. A resolved UNKNOWN keeps
        ``outcome_kind='unknown'`` for audit, and D3a resolves them routinely, so
        re-proving each one here would grow with history. It does not need to:
        acceptance proved every attempt at or before the fence (``settled`` /
        ``unresolved``), and the read's tail inventory proves the rest.
        """
        open_id = await session.scalar(select(ExecutionUncertaintyRow.uncertainty_id).where(
            *self._scope(ExecutionUncertaintyRow), ExecutionUncertaintyRow.symbol == symbol,
            ExecutionUncertaintyRow.state == "open").limit(1))
        if open_id is not None:
            raise CapitalBlockedError("execution_unknown")

    async def _effective_outcome(self, session: AsyncSession, attempt: SubmissionAttemptRow) -> str | None:
        if attempt.outcome_kind != "unknown":
            if attempt.outcome_kind not in {None, "acknowledged", "rejected", "not_sent"}:
                raise CapitalBlockedError("invalid_attempt_outcome")
            allowed_events = {
                None: {"RESERVATION_INTENT"},
                "acknowledged": {"RESERVATION_CLAIMED", "SUBMIT_MATCHED_TO_VENUE_OFFER",
                                 "UNCERTAINTY_BOUND_TO_VENUE_OFFER"},
                "rejected": {"RESERVATION_FAILED"}, "not_sent": {"RESERVATION_FAILED"},
            }
            evidence = await session.get(EventLogRow, attempt.last_event_seq)
            if evidence is None or evidence.event_type not in allowed_events[attempt.outcome_kind]:
                raise CapitalBlockedError("attempt_outcome_evidence_missing")
            if (evidence.exchange_account_id, evidence.deployment_environment) != (
                    self.account_id, self.environment):
                raise CapitalBlockedError("attempt_outcome_evidence_scope")
            decoded = deserialize_stored_event(evidence)
            if getattr(decoded, "symbol", None) != attempt.symbol:
                raise CapitalBlockedError("attempt_outcome_evidence_scope")
            if evidence.event_type != "UNCERTAINTY_BOUND_TO_VENUE_OFFER":
                ref = getattr(decoded, "reservation_ref", None)
                if ref is None or (ref.execution_decision_id, ref.cid) != (
                        attempt.execution_decision_id, attempt.cid):
                    raise CapitalBlockedError("attempt_outcome_evidence_identity")
            if attempt.outcome_kind in {"not_sent", "rejected"} and (
                (getattr(decoded, "reason", None) == "local_pre_transport")
                != (attempt.outcome_kind == "not_sent")
            ):
                raise CapitalBlockedError("attempt_outcome_evidence_conflict")
            return attempt.outcome_kind
        resolutions = (await session.scalars(select(ExecutionUncertaintyRow).where(
            *self._scope(ExecutionUncertaintyRow), ExecutionUncertaintyRow.symbol == attempt.symbol,
            ExecutionUncertaintyRow.attempt_id == attempt.attempt_id,
            ExecutionUncertaintyRow.kind == "submit_outcome_unknown"))).all()
        if len(resolutions) != 1 or resolutions[0].state != "resolved":
            raise CapitalBlockedError("execution_unknown")
        resolution = resolutions[0]
        logged = await session.get(EventLogRow, resolution.resolved_event_seq)
        if logged is None or (logged.exchange_account_id, logged.deployment_environment) != (
                self.account_id, self.environment):
            raise CapitalBlockedError("execution_unknown_resolution_missing")
        event = deserialize_stored_event(logged)
        if not isinstance(event, UncertaintyMarkedNotAccepted) or (
            event.uncertainty_id, event.account_id, event.environment, event.symbol,
            event.reconcile_event_seq, event.candidate_count, dict(event.resolution_evidence)
        ) != (resolution.uncertainty_id, str(self.account_id), self.environment, attempt.symbol,
              resolution.reconcile_event_seq, 0, resolution.resolution_evidence):
            raise CapitalBlockedError("execution_unknown_resolution_conflict")
        return "not_sent"  # Derived eligibility only; preserve historical UNKNOWN audit.

    async def begin_snapshot(self, session: AsyncSession, *, now_ms: int) -> UUID:
        """Persist query-start fence in a SHORT transaction; commit before fetching.

        A pending submit might cross a REST response, so no query is authoritative
        until it has a typed outcome. Existing no-policy reconciliation is unchanged.
        """
        await self._prepare(session)
        await self._assert_fenceable(session)
        query = CapitalQueryRow(id=uuid4(), exchange_account_id=self.account_id,
            deployment_environment=self.environment, command_fence=await self._fence(session),
            query_revision=int(await session.scalar(select(func.max(CapitalQueryRow.query_revision))
                .where(*self._scope(CapitalQueryRow))) or 0) + 1,
            started_at_ms=now_ms)
        session.add(query)
        await session.flush()
        return query.id

    async def accept_snapshot(self, session: AsyncSession, *, fence: UUID,
                              event: VenueSnapshotObserved, confirmation: VenueSnapshotObserved,
                              now_ms: int) -> SnapshotDrift:
        """Accept full observation only against its persisted pre-I/O fence.

        Caller must supply a consistent observation (the BootRecovery integration
        validates repeated REST observations); mere coverage is not atomicity.
        """
        await self._prepare(session)
        query = await session.get(CapitalQueryRow, fence)
        if query is None or (query.exchange_account_id, query.deployment_environment) != (
                self.account_id, self.environment):
            raise CapitalBlockedError("snapshot_query_scope")
        if (event.account_id, event.environment) != (str(self.account_id), self.environment):
            raise CapitalBlockedError("snapshot_scope")
        if query.command_fence != await self._fence(session):
            raise CapitalBlockedError("snapshot_command_fence_changed")
        latest_query = await self._latest_query(session)
        if latest_query != query.id:
            raise CapitalBlockedError("snapshot_query_superseded")
        if query.started_at_ms != event.query_started_at_ms:
            raise CapitalBlockedError("snapshot_query_start_mismatch")
        classification, blocked = await self._evaluate_observation(
            session, event=event, confirmation=confirmation, fence=query.command_fence,
            now_ms=now_ms,
        )
        observed = replace(event, capital_query_id=str(query.id),
            capital_command_fence=query.command_fence,
            capital_confirmation=serialize_event(confirmation),
            capital_classification_digest=_digest(classification))
        appended = await self._store.append_snapshot(session, observed)
        assert appended.event_seq is not None
        # Bind the classification to the ledger prefix it was derived from. The
        # writer sealed this row's chain link on append; an absent one means the
        # evidence event did not go through the writer, which cannot be trusted.
        evidence = await session.get(EventPrefixHashRow, appended.event_seq)
        if evidence is None:
            raise CapitalBlockedError("snapshot_prefix_unavailable")
        session.add(CapitalSnapshotRow(event_seq=appended.event_seq, query_id=query.id,
            exchange_account_id=self.account_id, deployment_environment=self.environment,
            schema_version=SCHEMA_VERSION, command_fence=query.command_fence,
            classification=classification, covered_prefix_hash=evidence.prefix_hash,
            authorization_blocked_reason=blocked))
        await session.flush()
        return appended

    async def _assert_fenceable(self, session: AsyncSession) -> None:
        """``begin_snapshot``'s refusals: an inconsistent inventory or an in-flight submit."""
        await self._attempt_inventory(session)
        pending = await session.scalar(select(SubmissionAttemptRow.attempt_id).where(
            *self._scope(SubmissionAttemptRow), SubmissionAttemptRow.outcome_kind.is_(None)).limit(1))
        if pending is not None:
            raise CapitalBlockedError("snapshot_inflight_command")

    async def _evaluate_observation(
        self, session: AsyncSession, *, event: VenueSnapshotObserved,
        confirmation: VenueSnapshotObserved, fence: int, now_ms: int,
    ) -> tuple[dict[str, Any], str | None]:
        """Validate a stable observation and classify it against ``fence``; reads only.

        Returns the classification and the historical-intent refusal acceptance
        records with it (``authorization_blocked_reason``). Shared by
        ``accept_snapshot`` and the read-only ``evaluate_observation_read_only``.
        """
        self._validate_snapshot(event, now_ms)
        self._validate_snapshot(confirmation, now_ms)
        if (confirmation.account_id, confirmation.environment) != (event.account_id, event.environment):
            raise CapitalBlockedError("snapshot_confirmation_scope")
        if confirmation.query_started_at_ms < event.query_finished_at_ms:
            raise CapitalBlockedError("snapshot_confirmation_overlaps")
        if self._observation(confirmation) != self._observation(event):
            raise CapitalBlockedError("snapshot_unstable")
        classification = await self._classify(session, event)
        # Prove the legacy intents here, where the fence is set, for every symbol a
        # read can later ask about. Doing it once at acceptance is what lets an
        # authorization read skip re-deriving the prefix without ever reporting
        # still-committed capital as available. A failure is recorded, not raised:
        # the observation itself still has to be stored, or the state that needs
        # settling can never be observed again.
        blocked: str | None = None
        try:
            await self._assert_historical_intents_settled(
                session, event=event, fence=fence,
                symbols=tuple(classification["symbols"]),
            )
        except CapitalBlockedError as exc:
            blocked = str(exc)
        return classification, blocked

    async def evaluate_observation_read_only(
        self, session: AsyncSession, *, event: VenueSnapshotObserved,
        confirmation: VenueSnapshotObserved, now_ms: int,
    ) -> ObservedAcceptance:
        """What ``begin_snapshot`` + ``accept_snapshot`` would accept now, without writing.

        Cutover comparison only (F3 (i'), deleted with the legacy authority): no
        query row, no event, no prefix link, no snapshot row, no account lock. The
        fence is the stream head in the caller's snapshot -- what ``begin_snapshot``
        would record with no writer running, which acceptance then requires to be
        unchanged. Raises ``CapitalBlockedError`` exactly where acceptance would.
        """
        await self._assert_fenceable(session)
        if (event.account_id, event.environment) != (str(self.account_id), self.environment):
            raise CapitalBlockedError("snapshot_scope")
        fence = await self._fence(session)
        classification, blocked = await self._evaluate_observation(
            session, event=event, confirmation=confirmation, fence=fence, now_ms=now_ms,
        )
        return ObservedAcceptance(event, fence, classification, blocked)

    async def read_observed(
        self, session: AsyncSession, acceptance: ObservedAcceptance, *, symbol: str,
        cell_id: str, now_ms: int, applied: AppliedCapitalPolicy,
    ) -> ObservedCapital:
        """``_read_capital`` over an ``evaluate_observation_read_only`` result; reads only.

        The same checks in the same order as a read right after ``accept_snapshot``
        stored that classification: open uncertainty of the symbol, the recorded
        historical-intent refusal, then ``_classified_basis`` and ``_fold_tail``.
        """
        await self._assert_no_unknown(session, symbol=symbol)
        if acceptance.blocked_reason is not None:
            raise CapitalBlockedError(acceptance.blocked_reason)
        basis = self._classified_basis(
            acceptance.classification, acceptance.event, command_fence=acceptance.command_fence,
            symbol=symbol, cell_id=cell_id, now_ms=now_ms,
        )
        snapshot, budget = await self._fold_tail(
            session, basis, symbol=symbol, cell_id=cell_id, applied=applied,
        )
        return ObservedCapital(applied, acceptance.command_fence, snapshot, budget,
                               basis.shared, acceptance.classification)

    async def _latest_query(self, session: AsyncSession) -> UUID | None:
        return cast(UUID | None, await session.scalar(select(CapitalQueryRow.id)
            .where(*self._scope(CapitalQueryRow))
            .order_by(CapitalQueryRow.query_revision.desc()).limit(1)))

    @staticmethod
    def _observation(event: VenueSnapshotObserved) -> dict[str, Any]:
        """Order-insensitive, identity-deduplicated stable REST observation.

        Two equal bounded observations are coherence evidence, not a venue
        atomicity claim. No wallet-total/lent assumption is made.
        """
        payload = serialize_event(event)
        return {"offers": {o["venue_offer_id"]: o for o in payload["offers"]},
                "credits": {c["credit_id"]: c for c in payload["credits"]},
                "wallet_available": payload["wallet_available"]}

    def _validate_snapshot(self, event: VenueSnapshotObserved, now_ms: int) -> None:
        coverage = event.coverage
        if not all(x is True for x in (coverage.active_offers_complete,
                coverage.active_credits_complete, coverage.wallets_complete)):
            raise CapitalBlockedError("snapshot_incomplete")
        if not (0 <= now_ms - event.query_started_at_ms <= self.max_snapshot_age_ms
                and event.query_started_at_ms <= event.query_finished_at_ms <= now_ms):
            raise CapitalBlockedError("snapshot_stale")
        for value in event.wallet_available.values():
            _amount(value)
        for objects, identity in ((event.offers, "venue_offer_id"), (event.credits, "credit_id")):
            ids: dict[str, object] = {}
            for obj in objects:
                key = getattr(obj, identity)
                if key in ids and ids[key] != obj:
                    raise CapitalBlockedError("snapshot_conflicting_identity")
                ids[key] = obj
                if obj.symbol not in event.wallet_available:
                    raise CapitalBlockedError("snapshot_missing_wallet")

    async def _classify(self, session: AsyncSession, event: VenueSnapshotObserved) -> dict[str, Any]:
        totals: dict[str, Any] = {symbol: {"available": str(_amount(amount)), "offered": "0", "credits": "0",
                          "unattributed_credits": "0", "foreign": "0", "cells": {}}
                  for symbol, amount in event.wallet_available.items()}
        reflected: dict[str, str] = {}
        foreign: dict[str, dict[str, str]] = {}
        # Our active offers, carried to the next snapshot as fill evidence.
        ours: dict[str, dict[str, Any]] = {}
        offers = {o.venue_offer_id: o for o in event.offers}
        for offer in offers.values():
            original, remaining = _amount(offer.amount_original), _amount(offer.amount_remaining)
            if offer.status not in {"active", "partially_filled"} or remaining <= ZERO or remaining > original:
                raise CapitalBlockedError("snapshot_invalid_active_offer")
            claims = (await session.scalars(select(OfferClaimRow).where(*self._scope(OfferClaimRow),
                OfferClaimRow.venue_offer_id == offer.venue_offer_id))).all()
            attempts = (await session.scalars(select(SubmissionAttemptRow).where(
                *self._scope(SubmissionAttemptRow),
                SubmissionAttemptRow.venue_offer_id == offer.venue_offer_id))).all()
            if not claims and not attempts:
                # Foreign (D2): no durable intent traces to it, so it is not ours
                # to count, cancel or reprice. Its amount already left the wallet's
                # ``available``; leaving it out of ``offered`` is what shrinks the
                # budget by exactly that much. Recorded so the reason is auditable.
                foreign[offer.venue_offer_id] = {"symbol": offer.symbol, "amount": str(remaining)}
                values = totals[offer.symbol]
                values["foreign"] = str(_amount(values["foreign"]) + remaining)
                continue
            if len(claims) != 1 or len(attempts) > 1:
                # Some provenance, but not exactly one story: an integrity fault,
                # never a reason to treat the offer as someone else's.
                raise CapitalBlockedError("offer_provenance_conflict")
            claim = claims[0]
            decision = await session.get(ExecutionDecisionRow, claim.execution_decision_id)
            if decision is None or (decision.exchange_account_id, decision.deployment_environment,
                    decision.symbol) != (self.account_id, self.environment, offer.symbol):
                raise CapitalBlockedError("offer_provenance_conflict")
            if claim.symbol != offer.symbol or _amount(claim.size_usdt) != original:
                raise CapitalBlockedError("offer_amount_conflict")
            if attempts:
                attempt = attempts[0]
                if (attempt.symbol != offer.symbol or attempt.outcome_kind != "acknowledged"
                        or attempt.execution_decision_id != decision.decision_id):
                    raise CapitalBlockedError("offer_attempt_conflict")
                reflected[str(attempt.attempt_id)] = offer.venue_offer_id
            values = totals[offer.symbol]
            values["offered"] = str(_amount(values["offered"]) + remaining)
            cells = values["cells"]
            cells[decision.cell_id] = str(_amount(cells.get(decision.cell_id, "0")) + remaining)
            ours[offer.venue_offer_id] = {"cell": decision.cell_id, "symbol": offer.symbol,
                                          "original": str(original), "remaining": str(remaining),
                                          "mts_created": offer.mts_created}
        previous = await session.scalar(select(CapitalSnapshotRow).where(
            *self._scope(CapitalSnapshotRow)).order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))
        prior = previous.classification if previous is not None else {}
        credit_cells = await self._attribute_credits(session, event, ours, prior)
        for credit in {c.credit_id: c for c in event.credits}.values():
            amount = _amount(credit.amount)
            if credit.status != "active" or amount <= ZERO:
                raise CapitalBlockedError("snapshot_invalid_active_credit")
            values = totals[credit.symbol]
            values["credits"] = str(_amount(values["credits"]) + amount)
            owners = credit_cells[credit.credit_id]["cells"]
            if not owners:
                # U: counts once in T (and, through the wallet, in spendable) and
                # in no cell -- charging it to every cell idled cash the venue
                # minimum no longer fit under the cap.
                values["unattributed_credits"] = str(_amount(values["unattributed_credits"]) + amount)
            cells = values["cells"]
            for cell in owners:
                # Each cell the credit may belong to carries all of it. Only an
                # ambiguous or not-yet-synced credit has more than one.
                cells[cell] = str(_amount(cells.get(cell, "0")) + amount)
        inventory = await self._attempt_inventory(session)
        prior_reflected = prior["reflected"] if previous is not None else {}
        history = {offer.venue_offer_id: offer for offer in event.offer_history}
        # An attempt that ended without spending holds no capital, but is never
        # reflected either. Record it, so a bounded read can tell it apart from an
        # unaccounted commitment by set membership instead of re-deriving it.
        settled: list[str] = []
        # An UNKNOWN still open holds its symbol, not the account: the attempt is
        # recorded against its symbol, and every read of that symbol refuses it
        # until a snapshot accepted after the resolution replaces this one.
        unresolved: dict[str, str] = {}
        for _, _, attempt in inventory.values():
            key = str(attempt.attempt_id)
            try:
                outcome = await self._effective_outcome(session, attempt)
            except CapitalBlockedError as exc:
                if str(exc) != "execution_unknown":
                    raise
                unresolved[key] = attempt.symbol
                continue
            if outcome in {"rejected", "not_sent"}:
                settled.append(key)
                continue
            if key in reflected:
                continue
            if attempt.outcome_kind == "acknowledged" and key in prior_reflected:
                # Previously proved debit; new complete snapshot accounts for
                # remaining offer/credit/cash without resurrecting original L.
                reflected[key] = prior_reflected[key]
                continue
            terminal = history.get(attempt.venue_offer_id or "")
            coverage = event.coverage
            if (attempt.outcome_kind == "acknowledged" and terminal is not None
                    and coverage.offer_history_complete
                    and coverage.offer_history_start_ms is not None
                    and coverage.offer_history_end_ms is not None
                    and coverage.offer_history_start_ms <= attempt.started_at_ms
                    and terminal.mts_updated <= coverage.offer_history_end_ms
                    and is_terminal_offer_status(terminal.status)
                    and terminal.status not in {"absent", "quarantined"}
                    and terminal.symbol == attempt.symbol
                    and _amount(terminal.amount_original) == _amount(attempt.normalized_payload.get("amount"))):
                # Exact acknowledged venue identity + terminal history. Matching
                # by fingerprinted amount/rate/period happens once, when an
                # UNKNOWN is resolved (boot recovery, D3a) -- never here, where
                # a credit would otherwise be assigned to a cell by resemblance.
                reflected[key] = terminal.venue_offer_id
                continue
            raise CapitalBlockedError("unclassifiable_commitment")
        return {"symbols": totals, "reflected": reflected, "settled": sorted(settled),
                "unresolved": unresolved, "foreign": foreign, "offers": ours,
                "credit_cells": credit_cells,
                "credit_attribution": ("a credit counts once in T and in the exposure of the "
                                       "cell its fill traces to; U is in T only")}

    async def _offer_cell(self, session: AsyncSession, venue_offer_id: str, symbol: str,
                          cache: dict[str, str | None]) -> str | None:
        """The cell whose execution decision placed this venue offer, or None.

        Same provenance chain as an active offer (claim -> decision -> cell). An
        offer no claim traces to, or a pre-decision claim, has no cell; a claim
        whose decision contradicts it is an integrity fault, not a foreign offer.
        """
        if venue_offer_id in cache:
            return cache[venue_offer_id]
        claims = (await session.scalars(select(OfferClaimRow).where(*self._scope(OfferClaimRow),
            OfferClaimRow.venue_offer_id == venue_offer_id))).all()
        if len(claims) > 1:
            raise CapitalBlockedError("credit_provenance_conflict")
        cell: str | None = None
        if claims and claims[0].execution_decision_id is not None:
            claim = claims[0]
            decision = await session.get(ExecutionDecisionRow, claim.execution_decision_id)
            if decision is None or claim.symbol != symbol or (
                    decision.exchange_account_id, decision.deployment_environment,
                    decision.symbol) != (self.account_id, self.environment, symbol):
                raise CapitalBlockedError("credit_provenance_conflict")
            cell = decision.cell_id
        cache[venue_offer_id] = cell
        return cell

    async def _attribute_credits(self, session: AsyncSession, event: VenueSnapshotObserved,
                                 ours: Mapping[str, Mapping[str, Any]],
                                 prior: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Which cell(s) each active credit's lent money belongs to, from the DB only.

        Decided once, here at the fence, and recorded in the classification, so
        the authorization read stays a lookup and never asks the venue.

        The unit is the group of active credits and loans sharing (symbol,
        period, MTS_OPENING): the venue stamps every record with its trade's
        instant as MTS_OPENING, while a loan turns into credits with new ids,
        new MTS_CREATE, split amounts and sometimes another rate, and a credit's
        rate carries more digits than its trade's (same key as the interest
        report, ``credit_attribution.assign_cells``). In order:

        1. ``funding_trade``: synced funding trades at the group's instant (trade
           MTS_CREATE == MTS_OPENING, same symbol and period) name our offers
           (OFFER_ID) -> decision -> cell, and together cover the group's amount.
           No pairing inside a group: every record carries every cell its trades
           name, the conservative answer for a cap. Trades naming no offer of
           ours leave the group unattributed. When the group holds more than
           the synced trades (a trade not synced yet), each record gets the
           trades' cells plus what steps 2-3 give it (``funding_trade_partial``).
        2. carried (keeps its basis): the previous accepted snapshot decided the
           group. Neither the id nor the amount survives a loan becoming credits,
           so the carry is by group; a fill the hourly trade sync has not reached
           keeps the cell it got when it first appeared, although the evidence
           that placed it (offer history) is fetched only that once. A previous
           snapshot recorded before groups carries by id and amount.
        3. ``recent_fill``: a record first seen now with no trade row yet goes to
           every cell with a fill of ours in this snapshot that could have
           produced it: same symbol, amount within the filled part, offer created
           no later than the record's opening. Rate and period are not required
           to match (a crossing fill takes the resting side's terms):
           over-charging a cell until the trade arrives is the safe direction,
           letting its lent money leave its exposure is not. Evidence: the
           filled part of our active offers, terminal offer history, and our
           offers of the previous snapshot now gone from the book (up to what
           was still offered).
        Otherwise the record is unattributed (U): in T only, in no cell.
        """
        credits = {c.credit_id: c for c in event.credits}
        cache: dict[str, str | None] = {}

        def opening(credit: VenueCreditObservation) -> int | None:
            # A snapshot recorded before MTS_OPENING was kept: MTS_CREATE is the
            # opening of any record that is not a converted loan.
            return credit.mts_opening if credit.mts_opening is not None else credit.mts_created

        group_of: dict[str, tuple[str, int, int]] = {}
        live: dict[tuple[str, int, int], Decimal] = {}
        for c in credits.values():
            at = opening(c)
            if c.period_days is not None and at is not None:
                key = (c.symbol, int(c.period_days), int(at))
                group_of[c.credit_id] = key
                live[key] = live.get(key, ZERO) + _amount(c.amount)
        traders: dict[tuple[str, int, int], set[str]] = {}
        traded: dict[tuple[str, int, int], Decimal] = {}
        stamps = sorted({key[2] for key in live})
        if stamps:
            # Rows returned are bounded by the active credits' openings; the scan
            # is the account's trade rows (PK prefix), which grow with fills only.
            # Add an (account, environment, mts_create) index once that is large.
            trades = (await session.scalars(select(FundingTradeRow).where(
                FundingTradeRow.exchange_account_id == self.account_id,
                FundingTradeRow.deployment_environment == self.environment,
                FundingTradeRow.mts_create.in_(stamps)))).all()
            for trade in trades:
                key = (trade.symbol, int(trade.period_days), int(trade.mts_create))
                if key not in live:
                    continue
                traded[key] = traded.get(key, ZERO) + abs(_amount(trade.amount))
                cells = traders.setdefault(key, set())
                cell = await self._offer_cell(session, str(trade.offer_id), trade.symbol, cache)
                if cell is not None:
                    cells.add(cell)
        # (cell, symbol, filled upper bound, offer creation) for fills of ours
        # visible at this fence.
        fills: list[tuple[str, str, Decimal, int | None]] = []
        for entry in ours.values():
            filled = _amount(entry["original"]) - _amount(entry["remaining"])
            if filled > ZERO:
                fills.append((entry["cell"], entry["symbol"], filled, entry["mts_created"]))
        for offer in event.offer_history:
            filled = _amount(offer.amount_original) - _amount(offer.amount_remaining)
            if (filled > ZERO and is_terminal_offer_status(offer.status)
                    and offer.status not in {"absent", "quarantined"}):
                cell = await self._offer_cell(session, offer.venue_offer_id, offer.symbol, cache)
                if cell is not None:
                    fills.append((cell, offer.symbol, filled, offer.mts_created))
        for venue_offer_id, entry in (prior.get("offers") or {}).items():
            if venue_offer_id not in ours:
                fills.append((entry["cell"], entry["symbol"], _amount(entry["remaining"]),
                              entry["mts_created"]))
        prior_cells: Mapping[str, Mapping[str, Any]] = prior.get("credit_cells") or {}
        carried_groups: dict[tuple[str, int, int], tuple[set[str], set[str]]] = {}
        for entry in prior_cells.values():
            if entry.get("opening") is not None and entry.get("period") is not None:
                owners_, bases = carried_groups.setdefault(
                    (entry["symbol"], int(entry["period"]), int(entry["opening"])), (set(), set()))
                owners_.update(entry["cells"])
                bases.add(entry["basis"])
        result: dict[str, dict[str, Any]] = {}
        for credit_id, credit in credits.items():
            amount = _amount(credit.amount)
            group = group_of.get(credit_id)
            owners: set[str]
            if group is not None and group in traded and live[group] <= traded[group]:
                owners, basis = set(traders[group]), "funding_trade"
            else:
                legacy = prior_cells.get(credit_id)
                if group is not None and group in carried_groups:
                    carried_owners, bases = carried_groups[group]
                    owners = set(carried_owners)
                    basis = next(iter(bases)) if len(bases) == 1 else "carried"
                elif (legacy is not None and legacy.get("opening") is None
                        and (legacy["symbol"], _amount(legacy["amount"])) == (credit.symbol, amount)):
                    owners, basis = set(legacy["cells"]), legacy["basis"]
                else:
                    at = opening(credit)
                    owners = {cell for cell, symbol, filled, created in fills
                              if symbol == credit.symbol and amount <= filled
                              and (created is None or at is None or created <= at)}
                    basis = "recent_fill" if owners else "none"
                if group is not None and group in traders:
                    # The group holds more than its synced trades: which record
                    # the synced ones produced is unknowable, so each carries both.
                    owners |= traders[group]
                    basis = "funding_trade_partial"
            result[credit_id] = {"symbol": credit.symbol, "amount": str(amount),
                                 "period": group[1] if group is not None else None,
                                 "opening": group[2] if group is not None else None,
                                 "cells": sorted(owners), "basis": basis}
        return result

    async def read_capital(self, session: AsyncSession, *, symbol: str, cell_id: str,
                           now_ms: int) -> CapitalView:
        applied = await self.read_applied(session, symbol=symbol)
        return await self._read_capital(session, symbol=symbol, cell_id=cell_id,
                                       now_ms=now_ms, applied=applied)

    async def preview_policy(self, session: AsyncSession, *, symbol: str, cell_id: str,
                             now_ms: int, policy: CapitalPolicy) -> CapitalView:
        """Conversion diagnostic only: synthetic revision 0 cannot authorize intents."""
        await self._prepare(session)
        return await self._read_capital(session, symbol=symbol, cell_id=cell_id, now_ms=now_ms,
            applied=AppliedCapitalPolicy(0, policy_digest(policy_payload(policy)), policy, UUID(int=0)))

    async def _read_capital(self, session: AsyncSession, *, symbol: str, cell_id: str,
                            now_ms: int, applied: AppliedCapitalPolicy) -> CapitalView:
        """Authorization read, bounded by construction.

        Commitments at or before the accepted fence cannot change the answer: the
        snapshot either accounts for them, or the fold below refuses. Re-deriving
        them asks an integrity question, which belongs to ``_read_capital_full`` on
        the audit path, not to a decision with a budget.

        Three things earn the right to skip that work, all decided when the fence
        was set: the classification names the ledger prefix it came from, every
        legacy intent was proved settled, and attempts that ended without spending
        were recorded. Without the third, a settled attempt has no intent in the
        tail and reads as an unaccounted commitment; deriving it here instead would
        put the unbounded work straight back.
        """
        row, basis = await self._snapshot_basis(
            session, symbol=symbol, cell_id=cell_id, now_ms=now_ms,
        )
        snapshot, budget = await self._fold_tail(
            session, basis, symbol=symbol, cell_id=cell_id, applied=applied,
        )
        return CapitalView(applied, row.event_seq, snapshot, budget, basis.shared, row.classification)

    async def _fold_tail(
        self, session: AsyncSession, basis: _SnapshotBasis, *, symbol: str, cell_id: str,
        applied: AppliedCapitalPolicy,
    ) -> tuple[CapitalSnapshot, CapitalBudget]:
        """Fold the bounded tail after the basis's fence into its snapshot and budget."""
        exposure, pending = basis.exposure, ZERO
        classification = basis.classification
        # Other symbols' unresolved attempts are accounted for too: the basis
        # already refused this symbol if one of them is its own.
        accounted = frozenset(classification["reflected"]) | frozenset(
            classification.get("settled", ())
        ) | frozenset(classification.get("unresolved", {}))
        inventory = await self._attempt_inventory(
            session, after_event_seq=basis.command_fence, reflected=accounted,
        )
        for _logged_intent, decoded, attempt in inventory.values():
            if decoded.symbol != symbol:
                continue
            if await self._effective_outcome(session, attempt) in {"rejected", "not_sent"}:
                continue
            amount = _amount(decoded.amount)
            if amount != _amount(attempt.normalized_payload.get("amount")):
                raise CapitalBlockedError("attempt_amount_conflict")
            pending += amount
            decision = await session.get(ExecutionDecisionRow, attempt.execution_decision_id)
            if decision is None or (decision.exchange_account_id, decision.deployment_environment,
                    decision.symbol) != (self.account_id, self.environment, symbol):
                raise CapitalBlockedError("attempt_decision_conflict")
            if decision.cell_id == cell_id:
                exposure += amount
        snapshot = CapitalSnapshot(basis.available, pending,
                                   basis.available + basis.offered + basis.credits, exposure)
        return snapshot, evaluate_capital(applied.policy, snapshot)

    async def _snapshot_basis(self, session: AsyncSession, *, symbol: str, cell_id: str,
                              now_ms: int) -> tuple[Any, _SnapshotBasis]:
        """Validate the accepted snapshot both reads derive their answer from.

        Every check here is a single indexed row or an equality on already-loaded
        content, so this part is bounded no matter how long the account has run.
        """
        await self._assert_no_unknown(session, symbol=symbol)
        row = await session.scalar(select(CapitalSnapshotRow).where(*self._scope(CapitalSnapshotRow))
            .order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))
        if row is None or row.schema_version != SCHEMA_VERSION:
            raise CapitalBlockedError("snapshot_unavailable")
        if await self._latest_query(session) != row.query_id:
            raise CapitalBlockedError("snapshot_query_pending")
        logged = await session.get(EventLogRow, row.event_seq)
        if logged is None:
            raise CapitalBlockedError("snapshot_evidence_missing")
        # One indexed row: the prefix this classification was derived from must still
        # be the prefix in the ledger. A missing binding or chain link is unproven,
        # not absent, so it blocks.
        chain = await session.get(EventPrefixHashRow, row.event_seq)
        if (row.covered_prefix_hash is None or chain is None
                or chain.prefix_hash != row.covered_prefix_hash):
            raise CapitalBlockedError("snapshot_prefix_diverged")
        if row.authorization_blocked_reason is not None:
            # Acceptance already decided this, with the same code the live proof
            # would raise. Reads keep failing closed without re-deriving it.
            raise CapitalBlockedError(row.authorization_blocked_reason)
        event = deserialize_stored_event(logged)
        if not isinstance(event, VenueSnapshotObserved):
            raise CapitalBlockedError("snapshot_evidence_invalid")
        query = await session.get(CapitalQueryRow, row.query_id)
        if query is None or (
            query.exchange_account_id, query.deployment_environment, query.command_fence,
            event.account_id, event.environment, event.capital_query_id,
            event.capital_command_fence, event.capital_classification_digest,
        ) != (self.account_id, self.environment, row.command_fence,
              str(self.account_id), self.environment, str(row.query_id), row.command_fence,
              _digest(row.classification)):
            raise CapitalBlockedError("snapshot_evidence_conflict")
        if event.capital_confirmation is None:
            raise CapitalBlockedError("snapshot_confirmation_missing")
        confirmation = deserialize_event("VENUE_SNAPSHOT_OBSERVED", dict(event.capital_confirmation))
        if not isinstance(confirmation, VenueSnapshotObserved) or (
            self._observation(confirmation) != self._observation(event)
        ):
            raise CapitalBlockedError("snapshot_confirmation_conflict")
        latest_observation = await session.scalar(select(func.max(EventLogRow.event_seq)).where(
            *self._scope(EventLogRow), EventLogRow.event_type == "VENUE_SNAPSHOT_OBSERVED"))
        if latest_observation != row.event_seq:
            raise CapitalBlockedError("snapshot_superseded_by_unfenced_observation")
        return row, self._classified_basis(
            row.classification, event, command_fence=row.command_fence, symbol=symbol,
            cell_id=cell_id, now_ms=now_ms,
        )

    def _classified_basis(self, classification: Mapping[str, Any], event: VenueSnapshotObserved,
                          *, command_fence: int, symbol: str, cell_id: str,
                          now_ms: int) -> _SnapshotBasis:
        """Freshness and the symbol's values of an accepted classification (no I/O)."""
        self._validate_snapshot(event, now_ms)
        if symbol not in classification["symbols"]:
            raise CapitalBlockedError("snapshot_symbol_missing")
        if symbol in classification.get("unresolved", {}).values():
            # Resolved since, perhaps -- but this snapshot was taken while it was
            # open and cannot say where that money is. The next one will.
            raise CapitalBlockedError("execution_unknown")
        values = classification["symbols"][symbol]
        available = _amount(values["available"])
        offered, credits = _amount(values["offered"]), _amount(values["credits"])
        shared = _amount(values["unattributed_credits"])
        # The cell's offers and the credits attributed to it at acceptance; U is
        # already in T via ``credits`` and in no cell.
        exposure = _amount(values["cells"].get(cell_id, "0"))
        if "credit_cells" not in classification:
            # Accepted before credits were attributed: its ``cells`` hold offers
            # only, so fall back to charging U to every cell until the next
            # snapshot replaces it. Remove once every deployed scope has accepted
            # a snapshot carrying ``credit_cells`` (the first reconcile after
            # this ships does it).
            exposure += shared
        return _SnapshotBasis(classification, command_fence, event, available, offered, credits,
                              shared, exposure)

    async def _read_capital_full(self, session: AsyncSession, *, symbol: str, cell_id: str,
                                 now_ms: int, applied: AppliedCapitalPolicy) -> CapitalView:
        """Audit definition: re-derive from the whole immutable history.

        No wall-clock budget applies here. This is what the bounded read is
        checked against, and what detects a prefix that stopped being true.
        """
        row, basis = await self._snapshot_basis(
            session, symbol=symbol, cell_id=cell_id, now_ms=now_ms,
        )
        event = basis.event
        available, offered, credits = basis.available, basis.offered, basis.credits
        shared, exposure = basis.shared, basis.exposure
        pending = ZERO
        inventory = await self._attempt_inventory(session)
        await self._assert_historical_intents_settled(
            session, event=event, fence=row.command_fence, symbols=(symbol,),
        )
        for logged_intent, decoded, attempt in inventory.values():
            if decoded.symbol != symbol:
                continue
            key = str(attempt.attempt_id)
            if await self._effective_outcome(session, attempt) in {"rejected", "not_sent"}:
                continue
            if key in row.classification["reflected"]:
                continue
            if logged_intent.event_seq <= row.command_fence:
                raise CapitalBlockedError("unclassifiable_commitment")
            amount = _amount(decoded.amount)
            if amount != _amount(attempt.normalized_payload.get("amount")):
                raise CapitalBlockedError("attempt_amount_conflict")
            pending += amount
            decision = await session.get(ExecutionDecisionRow, attempt.execution_decision_id)
            if decision is None or (decision.exchange_account_id, decision.deployment_environment,
                    decision.symbol) != (self.account_id, self.environment, symbol):
                raise CapitalBlockedError("attempt_decision_conflict")
            if decision.cell_id == cell_id:
                exposure += amount
        snapshot = CapitalSnapshot(available, pending, available + offered + credits, exposure)
        return CapitalView(applied, row.event_seq, snapshot,
                           evaluate_capital(applied.policy, snapshot),
                           shared, row.classification)

    async def _attempt_inventory(
        self, session: AsyncSession, *, after_event_seq: int | None = None,
        reflected: frozenset[str] | None = None,
    ) -> dict[str, tuple[EventLogRow, ReservationIntent, SubmissionAttemptRow]]:
        """Immutable intents define the universe, even with a current replay cursor.

        Verify both directions before deriving capital or accepting observations;
        missing/moved projections are corruption, never evidence of released cash.

        Unbounded by default: that is the audit reading. Given a fence and the
        commitments the accepted snapshot already reflects, both sides narrow to
        the same tail, so the two directions still have to agree -- over what is
        not yet accounted for rather than over all of history.
        """
        intents = select(EventLogRow).where(*self._scope(EventLogRow),
            EventLogRow.event_type == "RESERVATION_INTENT")
        if after_event_seq is not None:
            intents = intents.where(EventLogRow.event_seq > after_event_seq)
        rows = (await session.scalars(intents)).all()
        attempts = {str(a.attempt_id): a for a in (await session.scalars(
            select(SubmissionAttemptRow).where(*self._scope(SubmissionAttemptRow)))).all()}
        if reflected is not None:
            attempts = {key: value for key, value in attempts.items() if key not in reflected}
        inventory: dict[str, tuple[EventLogRow, ReservationIntent, SubmissionAttemptRow]] = {}
        for row in rows:
            event = deserialize_stored_event(row)
            if not isinstance(event, ReservationIntent):
                raise CapitalBlockedError("attempt_intent_conflict")
            payload = event.submission_attempt
            if not isinstance(payload, SubmissionAttemptPayload):
                continue
            key = str(payload.attempt_id)
            if key in inventory:
                raise CapitalBlockedError("duplicate_attempt_intent")
            attempt = attempts.get(key)
            if attempt is None:
                raise CapitalBlockedError("attempt_projection_missing")
            if (event.account_id, row.cid, event.execution_decision_id, event.symbol, event.cid) != (
                str(self.account_id), event.cid, payload.execution_decision_id, payload.symbol, payload.cid
            ) or (payload.account_id, payload.environment) != (self.account_id, self.environment):
                raise CapitalBlockedError("attempt_intent_scope_conflict")
            if (attempt.execution_decision_id, attempt.symbol, attempt.cid,
                attempt.normalized_payload, attempt.payload_sha256, attempt.started_at_ms) != (
                payload.execution_decision_id, payload.symbol, payload.cid,
                payload.as_storage_dict()["normalized_payload"], payload.payload_fingerprint,
                payload.started_at_ms
            ) or _amount(event.amount) != _amount(attempt.normalized_payload.get("amount")):
                raise CapitalBlockedError("attempt_projection_conflict")
            decision = await session.get(ExecutionDecisionRow, payload.execution_decision_id)
            if decision is None or (decision.exchange_account_id, decision.deployment_environment,
                decision.symbol, decision.signal_correlation_id, decision.amount_usdt) != (
                self.account_id, self.environment, event.symbol, str(event.signal_correlation_id),
                _amount(event.amount)
            ):
                raise CapitalBlockedError("attempt_decision_conflict")
            inventory[key] = row, event, attempt
        if inventory.keys() != attempts.keys():
            # Bounded: an unaccounted attempt whose intent is not in the tail has an
            # intent at or before the fence that the snapshot never reflected, which
            # is the same condition the full scan names unclassifiable_commitment.
            raise CapitalBlockedError(
                "unclassifiable_commitment" if after_event_seq is not None
                else "attempt_intent_missing"
            )
        return inventory

    async def _historical_cycles(self, session: AsyncSession) -> dict[int, list[EventLogRow]]:
        """One complete scoped proof per locked read, never a cross-read cache.

        Validate cross-CID venue ownership before indexing any cycle. Building
        the index in one pass also avoids quadratic scans for reused CIDs.
        """
        rows = (await session.scalars(select(EventLogRow).where(*self._scope(EventLogRow))
            .order_by(EventLogRow.event_seq))).all()
        try:
            historical_claim_reset_sequences(rows, account_id=str(self.account_id),
                                             environment=self.environment)
        except (ValueError, TypeError) as exc:
            raise CapitalBlockedError("unclassifiable_legacy_intent") from exc
        kinds = {"RESERVATION_INTENT", "RESERVATION_CLAIMED", "RESERVATION_FAILED",
                 "ORDER_FILL", "RESERVATION_RELEASED", "SUBMIT_OUTCOME_UNKNOWN",
                 "SUBMIT_MATCHED_TO_VENUE_OFFER"}
        cycles: dict[int, list[EventLogRow]] = {}
        current: dict[int | None, list[EventLogRow]] = {}
        for row in rows:
            if row.event_type == "RESERVATION_INTENT":
                # The full-scope validator already proved every reset boundary.
                current[row.cid] = cycles[row.event_seq] = []
            if row.event_type in kinds and row.cid in current:
                current[row.cid].append(row)
        return cycles

    async def _assert_historical_intents_settled(
        self, session: AsyncSession, *, event: VenueSnapshotObserved, fence: int,
        symbols: Collection[str],
    ) -> None:
        """Prove every legacy intent's cycle terminated at or before the fence.

        Legacy intents predate the attempt projection, so nothing else accounts for
        them. Capital they still hold must not read as available. This is the proof
        a bounded read is allowed to skip -- but only because it ran when the fence
        was set, which is why acceptance calls it too.
        """
        intents = (await session.scalars(select(EventLogRow).where(*self._scope(EventLogRow),
            EventLogRow.event_type == "RESERVATION_INTENT"))).all()
        historical_cycles: dict[int, list[EventLogRow]] | None = None
        for logged_intent in intents:
            decoded = deserialize_stored_event(logged_intent)
            if not isinstance(decoded, ReservationIntent) or decoded.symbol not in symbols:
                continue
            if isinstance(decoded.submission_attempt, SubmissionAttemptPayload):
                continue
            if historical_cycles is None:
                historical_cycles = await self._historical_cycles(session)
            await self._check_historical_intent(session, logged_intent, event, fence,
                historical_cycles.get(logged_intent.event_seq, ()))

    async def _check_historical_intent(self, session: AsyncSession, logged_intent: EventLogRow,
                                       snapshot: VenueSnapshotObserved, fence: int,
                                       cycle: Sequence[EventLogRow]) -> None:
        """Require THIS validated cycle's full-amount completion before the fence.

        A latest mutable claim or a partial fill cannot prove a terminal cycle.
        """
        intent = deserialize_stored_event(logged_intent)
        assert isinstance(intent, ReservationIntent)
        if not cycle or cycle[0].event_seq != logged_intent.event_seq or cycle[-1].event_seq > fence:
            raise CapitalBlockedError("unclassifiable_legacy_intent")
        for row in cycle:
            decoded = deserialize_stored_event(row)
            ref = getattr(decoded, "reservation_ref", None)
            decision = ref.execution_decision_id if ref else getattr(decoded, "execution_decision_id", None)
            if (row.schema_version != logged_intent.schema_version
                    or decision != intent.execution_decision_id
                    or _amount(getattr(decoded, "amount", None)) <= ZERO):
                raise CapitalBlockedError("unclassifiable_legacy_intent")
        shape = [r.event_type for r in cycle]
        if (shape == ["RESERVATION_INTENT", "RESERVATION_FAILED"]
                and all(r.venue_offer_id is None for r in cycle)):
            return
        if (shape in (["RESERVATION_INTENT", "RESERVATION_CLAIMED", "ORDER_FILL"],
                      ["RESERVATION_INTENT", "RESERVATION_CLAIMED", "RESERVATION_RELEASED"])
                and cycle[0].venue_offer_id is None and cycle[1].venue_offer_id):
            return  # Validator proved same venue, amount, symbol and correlation.
        if shape != ["RESERVATION_INTENT", "RESERVATION_CLAIMED"]:
            raise CapitalBlockedError("unclassifiable_legacy_intent")
        claim = await session.scalar(select(OfferClaimRow).where(*self._scope(OfferClaimRow),
            OfferClaimRow.cid == intent.cid, OfferClaimRow.symbol == intent.symbol))
        if claim is None or (claim.execution_decision_id, claim.signal_correlation_id,
                claim.venue_offer_id, claim.size_usdt, claim.last_event_seq) != (
                intent.execution_decision_id, str(intent.signal_correlation_id),
                cycle[-1].venue_offer_id, _amount(intent.amount), cycle[-1].event_seq):
            raise CapitalBlockedError("unclassifiable_legacy_intent")
        if claim.state == "claimed" and any(
                offer.venue_offer_id == claim.venue_offer_id and offer.symbol == intent.symbol
                for offer in snapshot.offers):
            return  # _classify already validated offer -> claim -> decision provenance.
        raise CapitalBlockedError("unclassifiable_legacy_intent")

    async def authorize_and_append_intent(
        self, session: AsyncSession, *, intent: ReservationIntent, decision: ExecutionDecisionRow,
        expected_revision: int, expected_digest: str, expected_snapshot_seq: int,
        now_ms: int, locked_guard: LockedGuard,
    ) -> AuthorizedIntent:
        """Capital check + immutable intent/evidence in caller's account transaction.

        REQUIRED callback rechecks live ownership/halt/eligibility in this exact
        session, without network I/O. It must raise on any unavailable guard.
        Capital alone never authorizes live execution. Caller commits then sends
        once; outcomes enter a separate transaction through the normal writer.
        """
        await self._prepare(session)
        attempt = intent.submission_attempt
        if not isinstance(attempt, SubmissionAttemptPayload) or (
            intent.account_id, attempt.environment, decision.exchange_account_id,
            decision.deployment_environment, decision.symbol, decision.decision_id,
            decision.signal_correlation_id,
        ) != (str(self.account_id), self.environment, self.account_id, self.environment,
              intent.symbol, intent.execution_decision_id, str(intent.signal_correlation_id)):
            raise CapitalBlockedError("intent_scope_conflict")
        amount = _amount(intent.amount)
        if (amount <= ZERO or amount != _amount(attempt.normalized_payload.get("amount"))
                or amount != _amount(decision.amount_usdt) or not decision.cell_id):
            raise CapitalBlockedError("intent_amount_conflict")
        if await session.get(SubmissionAttemptRow, attempt.attempt_id) is not None:
            raise CapitalBlockedError("attempt_already_committed")
        view = await self.read_capital(session, symbol=intent.symbol, cell_id=decision.cell_id, now_ms=now_ms)
        if (view.applied.revision, view.applied.digest) != (expected_revision, expected_digest):
            raise CapitalBlockedError("revision_changed")
        if view.snapshot_seq != expected_snapshot_seq:
            raise CapitalBlockedError("snapshot_changed")
        await locked_guard(session)
        if amount > view.budget.max_new_offer:
            raise CapitalBlockedError(view.budget.reason or "insufficient_deployable_funds")
        evidence = {"schema_version": SCHEMA_VERSION, "policy_revision": view.applied.revision,
            "policy_revision_id": str(view.applied.revision_id), "policy_digest": view.applied.digest,
            "snapshot_seq": view.snapshot_seq, "attempt_id": str(attempt.attempt_id),
            "cell_id": decision.cell_id, "amount": str(amount),
            "available": str(view.snapshot.available_amount),
            "unreflected": str(view.snapshot.unreflected_commitments),
            "cell_exposure": str(view.snapshot.cell_exposure),
            "unattributed_credit_exposure": str(view.unattributed_credit_exposure)}
        authorized = replace(intent, capital_authorization=evidence)
        session.add(decision)
        await session.flush()
        result = await self.writer.append(session, authorized)
        return AuthorizedIntent(result.event_seq, authorized, view)
