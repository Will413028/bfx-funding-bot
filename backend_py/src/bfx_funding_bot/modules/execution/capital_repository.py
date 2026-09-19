"""Transaction-scoped capital authority. This is not permission to call a venue.

Every public operation locks/replays the account stream. The caller owns the
transaction and the live ownership/halt/eligibility guard. In particular halt
writers MUST take the same account/environment xact lock (Task3). Commit before
any transport; never retry a committed intent after a crash.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_policy import (
    CapitalBudget,
    CapitalPolicy,
    CapitalSnapshot,
    evaluate_capital,
)
from bfx_funding_bot.modules.execution.capital_tables import (
    CapitalPolicyHeadRow,
    CapitalPolicyRevisionRow,
    CapitalQueryRow,
    CapitalSnapshotRow,
)
from bfx_funding_bot.modules.execution.event_store.entities import (
    is_terminal_offer_status,
)
from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    deserialize_stored_event,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore, SnapshotDrift
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow, OfferClaimRow
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

ZERO = Decimal("0")
SCHEMA_VERSION = 1
LockedGuard = Callable[[AsyncSession], Awaitable[None]]


class CapitalBlockedError(ValueError):
    """No authorization was issued; caller must not submit."""


def policy_payload(policy: CapitalPolicy) -> dict[str, Any]:
    return {"enabled": policy.enabled, "reserve_amount": str(policy.reserve_amount),
            "allocation_mode": policy.allocation_mode,
            "max_cell_fraction": str(policy.max_cell_fraction)}


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
        if symbol not in {"fUST", "fUSD"} or (symbol == "fUSD" and policy.enabled):
            raise CapitalBlockedError("unsupported_enabled_symbol")
        head = await session.get(CapitalPolicyHeadRow, (self.account_id, self.environment, symbol),
                                 populate_existing=True)
        version = head.revision if head is not None else 0
        if type(expected_revision) is not int or expected_revision != version:
            raise CapitalBlockedError("revision_changed")
        payload = policy_payload(policy)
        row = CapitalPolicyRevisionRow(
            id=uuid4(), exchange_account_id=self.account_id, deployment_environment=self.environment,
            symbol=symbol, revision=version + 1, schema_version=SCHEMA_VERSION, policy=payload,
            digest=_digest(payload), source=source,
        )
        session.add(row)
        await session.flush()
        if head is None:
            session.add(CapitalPolicyHeadRow(exchange_account_id=self.account_id,
                deployment_environment=self.environment, symbol=symbol, revision_id=row.id,
                revision=row.revision))
        else:
            head.revision_id, head.revision = row.id, row.revision
        await session.flush()
        return AppliedCapitalPolicy(row.revision, row.digest, policy, row.id)

    async def read_applied(self, session: AsyncSession, *, symbol: str) -> AppliedCapitalPolicy:
        await self._prepare(session)
        head = await session.get(CapitalPolicyHeadRow, (self.account_id, self.environment, symbol),
                                 populate_existing=True)
        if head is None:
            raise CapitalBlockedError("policy_unavailable")
        row = await session.get(CapitalPolicyRevisionRow, head.revision_id, populate_existing=True)
        if row is None or (row.exchange_account_id, row.deployment_environment, row.symbol,
                           row.revision) != (self.account_id, self.environment, symbol, head.revision):
            raise CapitalBlockedError("inconsistent_policy_pointer")
        if row.schema_version != SCHEMA_VERSION or row.digest != _digest(row.policy):
            raise CapitalBlockedError("invalid_policy_schema_or_digest")
        try:
            if set(row.policy) != {"enabled", "reserve_amount", "allocation_mode", "max_cell_fraction"}:
                raise ValueError
            policy = CapitalPolicy(enabled=row.policy["enabled"],
                reserve_amount=_amount(row.policy["reserve_amount"]),
                allocation_mode=row.policy["allocation_mode"],
                max_cell_fraction=_amount(row.policy["max_cell_fraction"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise CapitalBlockedError("invalid_policy") from exc
        return AppliedCapitalPolicy(row.revision, row.digest, policy, row.id)

    async def _fence(self, session: AsyncSession) -> int:
        # Conservative stream fence includes cancel, outcomes, WS and reconciles.
        return int(await session.scalar(select(func.max(EventLogRow.event_seq)).where(
            *self._scope(EventLogRow))) or 0)

    async def _assert_no_unknown(self, session: AsyncSession) -> None:
        open_id = await session.scalar(select(ExecutionUncertaintyRow.uncertainty_id).where(
            *self._scope(ExecutionUncertaintyRow), ExecutionUncertaintyRow.state == "open").limit(1))
        if open_id is not None:
            raise CapitalBlockedError("execution_unknown")
        unknown = (await session.scalars(select(SubmissionAttemptRow).where(
            *self._scope(SubmissionAttemptRow), SubmissionAttemptRow.outcome_kind == "unknown"))).all()
        for attempt in unknown:
            await self._effective_outcome(session, attempt)

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
        await self._assert_no_unknown(session)
        pending = await session.scalar(select(SubmissionAttemptRow.attempt_id).where(
            *self._scope(SubmissionAttemptRow), SubmissionAttemptRow.outcome_kind.is_(None)).limit(1))
        if pending is not None:
            raise CapitalBlockedError("snapshot_inflight_command")
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
        self._validate_snapshot(event, now_ms)
        self._validate_snapshot(confirmation, now_ms)
        if (confirmation.account_id, confirmation.environment) != (event.account_id, event.environment):
            raise CapitalBlockedError("snapshot_confirmation_scope")
        if confirmation.query_started_at_ms < event.query_finished_at_ms:
            raise CapitalBlockedError("snapshot_confirmation_overlaps")
        if self._observation(confirmation) != self._observation(event):
            raise CapitalBlockedError("snapshot_unstable")
        await self._assert_no_unknown(session)
        classification = await self._classify(session, event)
        observed = replace(event, capital_query_id=str(query.id),
            capital_command_fence=query.command_fence,
            capital_confirmation=serialize_event(confirmation),
            capital_classification_digest=_digest(classification))
        appended = await self._store.append_snapshot(session, observed)
        assert appended.event_seq is not None
        session.add(CapitalSnapshotRow(event_seq=appended.event_seq, query_id=query.id,
            exchange_account_id=self.account_id, deployment_environment=self.environment,
            schema_version=SCHEMA_VERSION, command_fence=query.command_fence,
            classification=classification))
        await session.flush()
        return appended

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
                          "unattributed_credits": "0", "cells": {}}
                  for symbol, amount in event.wallet_available.items()}
        reflected: dict[str, str] = {}
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
            if len(claims) != 1 or len(attempts) > 1:
                raise CapitalBlockedError("unattributed_offer")
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
        # Credits have no genuine cell link in this schema. U is a shared upper
        # bound charged to EVERY cell; never sum cell exposures into account T.
        for credit in {c.credit_id: c for c in event.credits}.values():
            amount = _amount(credit.amount)
            if credit.status != "active" or amount <= ZERO:
                raise CapitalBlockedError("snapshot_invalid_active_credit")
            values = totals[credit.symbol]
            values["credits"] = str(_amount(values["credits"]) + amount)
            values["unattributed_credits"] = values["credits"]
        attempts = (await session.scalars(select(SubmissionAttemptRow).where(
            *self._scope(SubmissionAttemptRow)))).all()
        previous = await session.scalar(select(CapitalSnapshotRow).where(
            *self._scope(CapitalSnapshotRow)).order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))
        prior_reflected = previous.classification["reflected"] if previous is not None else {}
        history = {offer.venue_offer_id: offer for offer in event.offer_history}
        for attempt in attempts:
            key = str(attempt.attempt_id)
            if await self._effective_outcome(session, attempt) in {"rejected", "not_sent"}:
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
                # Exact acknowledged venue identity + terminal history, never
                # amount/rate/time matching of an unrelated credit to a cell.
                reflected[key] = terminal.venue_offer_id
                continue
            raise CapitalBlockedError("unclassifiable_commitment")
        return {"symbols": totals, "reflected": reflected,
                "credit_attribution": "U is conservative shared exposure for every cell; counted once in T"}

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
            applied=AppliedCapitalPolicy(0, _digest(policy_payload(policy)), policy, UUID(int=0)))

    async def _read_capital(self, session: AsyncSession, *, symbol: str, cell_id: str,
                            now_ms: int, applied: AppliedCapitalPolicy) -> CapitalView:
        await self._assert_no_unknown(session)
        row = await session.scalar(select(CapitalSnapshotRow).where(*self._scope(CapitalSnapshotRow))
            .order_by(CapitalSnapshotRow.event_seq.desc()).limit(1))
        if row is None or row.schema_version != SCHEMA_VERSION:
            raise CapitalBlockedError("snapshot_unavailable")
        if await self._latest_query(session) != row.query_id:
            raise CapitalBlockedError("snapshot_query_pending")
        logged = await session.get(EventLogRow, row.event_seq)
        if logged is None:
            raise CapitalBlockedError("snapshot_evidence_missing")
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
        self._validate_snapshot(event, now_ms)
        if symbol not in row.classification["symbols"]:
            raise CapitalBlockedError("snapshot_symbol_missing")
        values = row.classification["symbols"][symbol]
        available = _amount(values["available"])
        offered, credits = _amount(values["offered"]), _amount(values["credits"])
        shared = _amount(values["unattributed_credits"])
        exposure = _amount(values["cells"].get(cell_id, "0")) + shared
        pending = ZERO
        attempts = (await session.scalars(select(SubmissionAttemptRow).where(
            *self._scope(SubmissionAttemptRow), SubmissionAttemptRow.symbol == symbol))).all()
        intents = (await session.scalars(select(EventLogRow).where(*self._scope(EventLogRow),
            EventLogRow.event_type == "RESERVATION_INTENT"))).all()
        by_attempt: dict[str, tuple[EventLogRow, ReservationIntent]] = {}
        for logged_intent in intents:
            decoded = deserialize_stored_event(logged_intent)
            if not isinstance(decoded, ReservationIntent) or decoded.symbol != symbol:
                continue
            if not isinstance(decoded.submission_attempt, SubmissionAttemptPayload):
                await self._check_historical_intent(session, decoded, event, row.command_fence)
                continue
            key = str(decoded.submission_attempt.attempt_id)
            if key in by_attempt:
                raise CapitalBlockedError("duplicate_attempt_intent")
            by_attempt[key] = logged_intent, decoded
        for attempt in attempts:
            key = str(attempt.attempt_id)
            pair = by_attempt.get(key)
            if pair is None:
                raise CapitalBlockedError("attempt_intent_missing")
            logged_intent, decoded = pair
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
        return CapitalView(applied, row.event_seq, snapshot, evaluate_capital(applied.policy, snapshot),
                           shared, row.classification)

    async def _check_historical_intent(self, session: AsyncSession, intent: ReservationIntent,
                                       snapshot: VenueSnapshotObserved, fence: int) -> None:
        """Old completed event identities are history, never legacy capital settings.

        Ambiguous historical pending/unknown still block. Cancellation alone is
        not cash: its terminal event must precede the accepted query fence.
        """
        claim = await session.scalar(select(OfferClaimRow).where(*self._scope(OfferClaimRow),
            OfferClaimRow.cid == intent.cid, OfferClaimRow.symbol == intent.symbol))
        if claim is None or (claim.execution_decision_id, claim.signal_correlation_id) != (
                intent.execution_decision_id, str(intent.signal_correlation_id)):
            raise CapitalBlockedError("unclassifiable_legacy_intent")
        terminal = await session.get(EventLogRow, claim.last_event_seq)
        if terminal is None or terminal.event_seq > fence:
            raise CapitalBlockedError("unclassifiable_legacy_intent")
        if claim.state == "failed" and terminal.event_type == "RESERVATION_FAILED":
            return
        if claim.state == "released" and terminal.event_type == "RESERVATION_RELEASED":
            return
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
