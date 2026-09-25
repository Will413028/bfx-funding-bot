"""Release flow by change class: the boot gate, operator requests, probation.

ADR 2026-09-25 D1-D4, plan §1 "Release flow":

- **Boot gate.** The deploy tool injects ``BFX_CHANGE_CLASS``,
  ``BFX_IMAGE_DIGEST``, ``BFX_SOURCE_REVISION`` and ``BFX_DEPLOYMENT_ID`` (the
  ``attempt_id`` of the ``started`` row it appended to the ``deployments``
  ledger before creating the containers). The class a build runs under is not
  the pairwise class against the previous deploy but the *unabsorbed material*
  behind it: walking the deployments whose code this build contains (every
  attempt that was ``deployed``, then this one), any material change keeps the
  build material until a later build is *accepted* -- its digest approved and
  its probation lifted. So a material X deployed while stopped, then a standard
  Y on top, still needs an approval before Y trades. A *standard* build keeps
  the trading state it found; a *material* one whose digest has no approval
  parks the writer in ``REDUCING`` (cause ``material_deploy``). No, malformed or
  unrecorded deploy identity (a local run, an old deploy path, no ledger row for
  this deployment) is material -- fail closed.
- **Requests.** The web API only inserts an approve, resume, pause or kill
  request (the operator-request outbox); the :class:`TradingControlWorker`
  applies it under the account lock after re-checking the operator, and records
  one outcome on the row. A kill's venue cancel-all runs after that commit.
- **Probation.** An approval, and a resume after an automatic HALTED, start a
  probation: 25% of the normal cell limit, floored at one venue-minimum offer.
  It lifts automatically after 24 hours with at least three acknowledged
  submits and no HALTED in between. Until it passes, every return to ACTIVE
  stays inside it (``trading_state.unfinished_probation``): after an automatic
  stop a new probation starts; after a pause or an operator's own stop the same
  limits restart. A maintenance pause outside any probation resumes without one.
  Only a transition that starts a new probation observes the venue minimum.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.external.bitfinex.funding_rules import FundingRuleProvider, submit_amount
from bfx_funding_bot.modules.deployments.tables import DeploymentRow
from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    REJECTED,
    REQUESTED,
    NeedsPreparation,
    OperatorAuthority,
    OperatorRequestWorker,
    Outcome,
    RequestRejected,
)
from bfx_funding_bot.modules.execution.safety.tables import (
    DeploymentApprovalRow,
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import (
    ACTIVE,
    CAUSE_AUTO,
    CAUSE_MATERIAL_DEPLOY,
    CAUSE_OPERATOR,
    HALTED,
    PROBATION_LIFTED,
    REDUCING,
    IllegalTradingTransition,
    Probation,
    TradingState,
    TransitionResult,
    announce,
    append_transition,
    read_current,
    unfinished_probation,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

STANDARD = "standard"
MATERIAL = "material"
PROBATION_MULTIPLIER = Decimal("0.25")
BAKE_MS = 24 * 60 * 60 * 1000
BAKE_MIN_ACKNOWLEDGED = 3

# Alert events (modules/observability/alerts); trading_state adds
# trading_state_changed / probation_started for every committed transition.
DEPLOY_GATE = "deploy_gate"
TRADING_CONTROL_APPLIED = "trading_control_applied"
TRADING_CONTROL_REJECTED = "trading_control_rejected"
TRADING_CONTROL_FAILED = "trading_control_failed"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    """What the deploy tool said this process is. ``problem`` makes it material."""

    backend_digest: str | None
    source_revision: str | None
    change_class: str
    problem: str | None = None
    # The ledger attempt this process was started by (BFX_DEPLOYMENT_ID).
    deployment_id: UUID | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DeploymentIdentity:
        digest = (env.get("BFX_IMAGE_DIGEST") or "").strip()
        revision = (env.get("BFX_SOURCE_REVISION") or "").strip()
        klass = (env.get("BFX_CHANGE_CLASS") or "").strip()
        raw_id = (env.get("BFX_DEPLOYMENT_ID") or "").strip()
        if not (digest or revision or klass or raw_id):
            return cls(None, None, MATERIAL, "deployment_identity_missing")
        try:
            deployment_id: UUID | None = UUID(raw_id)
        except ValueError:
            deployment_id = None
        if not _DIGEST.fullmatch(digest) or not _REVISION.fullmatch(revision) or deployment_id is None:
            return cls(digest if _DIGEST.fullmatch(digest) else None,
                       revision if _REVISION.fullmatch(revision) else None,
                       MATERIAL, "deployment_identity_invalid", deployment_id)
        if klass not in {STANDARD, MATERIAL}:
            return cls(digest, revision, MATERIAL, "change_class_invalid", deployment_id)
        return cls(digest, revision, klass, None, deployment_id)


async def digest_accepted(session: AsyncSession, *, account_id: UUID, environment: str,
                          digest: str) -> bool:
    """Approved once, and a probation lifted after that approval."""
    approved_at = await session.scalar(select(DeploymentApprovalRow.approved_at_ms).where(
        DeploymentApprovalRow.exchange_account_id == account_id,
        DeploymentApprovalRow.deployment_environment == environment,
        DeploymentApprovalRow.backend_digest == digest,
    ))
    if approved_at is None:
        return False
    return await session.scalar(select(TradingStateRow.id).where(
        TradingStateRow.exchange_account_id == account_id,
        TradingStateRow.deployment_environment == environment,
        TradingStateRow.state == ACTIVE, TradingStateRow.cause == CAUSE_AUTO,
        TradingStateRow.probation_multiplier.is_(None),
        TradingStateRow.created_at_ms >= approved_at,
    ).limit(1)) is not None


async def effective_change_class(session: AsyncSession, identity: DeploymentIdentity, *,
                                 account_id: UUID, environment: str) -> tuple[str, str]:
    """The class this build runs under, and why: the unabsorbed material behind it.

    Pairwise classes only compare each deploy with the one before it, so a
    material change deployed while stopped would be forgotten by the next
    standard deploy. Instead, walk the deployments whose code this build
    contains -- every attempt that ended ``deployed`` before this one, then
    this one -- and let any material change stand until a later build (or this
    one) was accepted: its digest approved and its probation lifted.
    """
    if identity.problem is not None or identity.backend_digest is None:
        return MATERIAL, identity.problem or "deployment_identity_missing"
    rows = (await session.scalars(select(DeploymentRow).order_by(DeploymentRow.id))).all()
    own = [row for row in rows if row.attempt_id == identity.deployment_id]
    if not own:
        return MATERIAL, "deployment_ledger_row_missing"
    if any((row.backend_digest, row.source_revision) != (identity.backend_digest,
                                                         identity.source_revision) for row in own):
        return MATERIAL, "ledger_identity_conflict"
    position = own[0].id
    attempts: dict[UUID, list[DeploymentRow]] = {}
    for row in rows:
        if row.id < position:
            attempts.setdefault(row.attempt_id, []).append(row)
    chain = [group[0] for group in attempts.values()
             if any(row.outcome == "deployed" for row in group)]
    chain.append(own[0])
    pending: str | None = None
    for row in chain:
        klass = row.change_class
        if row is own[0] and identity.change_class == MATERIAL:
            klass = MATERIAL
        if klass == MATERIAL:
            pending = row.backend_digest
        if pending is not None and await digest_accepted(
                session, account_id=account_id, environment=environment, digest=row.backend_digest):
            pending = None
    if pending is None:
        return STANDARD, "no_unabsorbed_material"
    if pending == identity.backend_digest:
        return MATERIAL, "material_build"
    return MATERIAL, f"unabsorbed_material:{pending}"


async def is_approved(session: AsyncSession, *, account_id: UUID, environment: str,
                      digest: str | None) -> bool:
    if digest is None:
        return False
    return await session.scalar(select(DeploymentApprovalRow.id).where(
        DeploymentApprovalRow.exchange_account_id == account_id,
        DeploymentApprovalRow.deployment_environment == environment,
        DeploymentApprovalRow.backend_digest == digest,
    ).limit(1)) is not None


@dataclass(frozen=True, slots=True)
class GateDecision:
    change_class: str
    why: str
    approved: bool
    action: str  # "kept" | "reducing" | "already_reducing" | "behind_stop"
    state: TradingState | None


async def apply_deploy_gate(
    session_factory: async_sessionmaker[AsyncSession], *, account_id: UUID, environment: str,
    identity: DeploymentIdentity, now_ms: int,
) -> GateDecision:
    """Run once at boot, before anything can trade."""
    async with session_factory.begin() as session:
        await acquire_transaction_lock(session, account_id=str(account_id),
                                       deployment_environment=environment)
        klass, why = await effective_change_class(session, identity, account_id=account_id,
                                                  environment=environment)
        approved = await is_approved(session, account_id=account_id, environment=environment,
                                     digest=identity.backend_digest)
        current = await read_current(session, account_id=account_id, environment=environment)
        result_to_announce: TransitionResult | None = None
        if klass == STANDARD or approved:
            action = "kept"
        elif current is None or current.state == HALTED:
            # A stop stays a stop; resuming it will require this approval.
            action = "behind_stop"
        elif current.state == REDUCING and current.cause == CAUSE_MATERIAL_DEPLOY:
            action = "already_reducing"
        else:
            revision = (identity.source_revision or "unknown")[:12]
            result = await append_transition(
                session, account_id=account_id, environment=environment, state=REDUCING,
                cause=CAUSE_MATERIAL_DEPLOY, actor=f"deploy:{revision}",
                reason=(f"material deploy {identity.backend_digest or 'unidentified'} "
                        f"({why}) awaiting approval"),
                now_ms=now_ms,
            )
            current, action, result_to_announce = result.state, "reducing", result
    log.warning("deploy_gate class=%s why=%s approved=%s action=%s digest=%s", klass, why,
                approved, action, identity.backend_digest)
    # Committed. A material build waiting for approval is what the operator must act on.
    alerts.emit(DEPLOY_GATE, level=alerts.INFO if action == "kept" else alerts.WARNING,
                action=action, change_class=klass, why=why, approved=approved,
                digest=identity.backend_digest or "unidentified",
                revision=(identity.source_revision or "unknown")[:12],
                trading_state=current.state if current is not None else "none")
    if result_to_announce is not None:
        announce(result_to_announce)
    return GateDecision(klass, why, approved, action, current)


@dataclass(frozen=True, slots=True)
class ProbationProgress:
    """How far a probation is toward its lift (ADR D3)."""

    started_at_ms: int
    elapsed_ms: int
    acknowledged: int
    required_ms: int
    required_acknowledged: int

    @property
    def passed(self) -> bool:
        return self.elapsed_ms >= self.required_ms and self.acknowledged >= self.required_acknowledged


async def probation_progress(session: AsyncSession, *, account_id: UUID, environment: str,
                             probation: Probation, now_ms: int, bake_ms: int = BAKE_MS,
                             bake_acknowledged: int = BAKE_MIN_ACKNOWLEDGED) -> ProbationProgress:
    """Elapsed time and acknowledged submits since the probation (re)started.

    No HALTED can have happened inside it: a HALTED supersedes the probation,
    so a current row that still carries one proves there was none.
    """
    started = probation.started_at_ms
    acknowledged = int(await session.scalar(select(func.count()).select_from(
        SubmissionAttemptRow).where(
            SubmissionAttemptRow.exchange_account_id == account_id,
            SubmissionAttemptRow.deployment_environment == environment,
            SubmissionAttemptRow.outcome_kind == "acknowledged",
            SubmissionAttemptRow.started_at_ms >= started,
        )) or 0)
    return ProbationProgress(started_at_ms=started, elapsed_ms=max(0, now_ms - started),
                             acknowledged=acknowledged, required_ms=bake_ms,
                             required_acknowledged=bake_acknowledged)


class _Kill(Protocol):
    async def engage(self, *, cause: str, actor: str, reason: str,
                     when_already_halted: str = "retry") -> Any: ...


class TradingControlWorker(OperatorRequestWorker[TradingControlRequestRow, dict[str, Decimal]]):
    """Applies approve/resume/pause/kill requests and lifts a probation that has passed.

    A kill writes HALTED (cause operator) in the request's transaction, then --
    after commit, outside every lock -- runs the kill switch, which repeats
    nothing but the venue cancel-all (audited and alerted like /admin/halt).
    Asking again retries an incomplete cancel-all, as /admin/halt does.
    """

    model = TradingControlRequestRow
    name = "trading_control"

    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], account_id: UUID,
        environment: str, identity: DeploymentIdentity, symbols: Iterable[str],
        funding_rules: FundingRuleProvider | None, authority: OperatorAuthority,
        clock: Callable[[], int] | None = None,
        ownership: Callable[[], Awaitable[bool]] | None = None, poll_interval_s: float = 5.0,
        multiplier: Decimal = PROBATION_MULTIPLIER, bake_ms: int = BAKE_MS,
        bake_acknowledged: int = BAKE_MIN_ACKNOWLEDGED,
    ) -> None:
        super().__init__(session_factory=session_factory, account_id=account_id,
                         environment=environment, authority=authority, clock=clock,
                         ownership=ownership, poll_interval_s=poll_interval_s)
        self.identity = identity
        self._symbols = tuple(sorted(set(symbols)))
        self._funding_rules = funding_rules
        self._multiplier = multiplier
        self._bake_ms = bake_ms
        self._bake_acknowledged = bake_acknowledged
        # Bound by the daemon once the kill switch exists (it is built later).
        self.kill_switch: _Kill | None = None

    def queue_order(self) -> tuple[Any, ...]:
        """A kill never waits behind another request."""
        return (case((TradingControlRequestRow.action == "kill", 0), else_=1),
                TradingControlRequestRow.created_at_ms, TradingControlRequestRow.request_id)

    async def idle(self) -> None:
        await self.lift_probation_if_passed()

    async def prepare(self, row_id: UUID) -> dict[str, Decimal]:
        """One venue-minimum offer per currency, observed now: the probation floor.

        Only a decision that starts a new probation asks for it (raising
        :class:`NeedsPreparation`), so no other request depends on the venue.
        """
        if self._funding_rules is None:
            raise RequestRejected("funding_rule_unavailable")
        floors: dict[str, Decimal] = {}
        for symbol in self._symbols:
            evidence = await self._funding_rules.observe(symbol)
            floors[symbol] = submit_amount(evidence, symbol=symbol, now_ms=self.clock())
        return floors

    async def apply(self, session: AsyncSession, row: TradingControlRequestRow,
                    prepared: dict[str, Decimal] | None) -> Outcome:
        try:
            result, note = await self._decide(session, row, prepared)
        except IllegalTradingTransition as exc:
            raise RequestRejected(f"illegal_transition: {exc}") from exc
        return Outcome(APPLIED, note, columns={
            "trading_state_id": result.state.id if result is not None else None,
        }, detail=result)

    async def committed(self, row: TradingControlRequestRow, outcome: Outcome) -> None:
        """Tell the operator, then finish a kill at the venue (outside every lock)."""
        fields = {"request_id": str(row.request_id), "action": row.action, "by": row.requested_by}
        if outcome.state == APPLIED:
            result = outcome.detail if isinstance(outcome.detail, TransitionResult) else None
            alerts.emit(TRADING_CONTROL_APPLIED, level=alerts.WARNING, outcome=outcome.reason,
                        state_id=result.state.id if result is not None else "unchanged", **fields)
            if result is not None:
                announce(result)
            if row.action == "kill":
                await self._cancel_all(row)
        elif outcome.state == REJECTED:
            alerts.emit(TRADING_CONTROL_REJECTED, level=alerts.WARNING, reason=outcome.reason,
                        **fields)
        else:
            alerts.emit(TRADING_CONTROL_FAILED, level=alerts.CRITICAL, reason=outcome.reason,
                        **fields)

    async def _cancel_all(self, row: TradingControlRequestRow) -> None:
        if self.kill_switch is None:
            log.critical("trading_control_kill_without_kill_switch request=%s", row.request_id)
            alerts.emit(TRADING_CONTROL_FAILED, level=alerts.CRITICAL, request_id=str(row.request_id),
                        action=row.action, reason="kill switch not wired: venue offers not cancelled")
            return
        # HALTED is already committed; the kill switch restates it and runs
        # the venue cancel-all, recording and alerting its outcome.
        await self.kill_switch.engage(cause=CAUSE_OPERATOR, actor=row.requested_by,
                                      reason=f"kill: {row.reason}", when_already_halted="retry")

    async def _decide(self, session: AsyncSession, row: TradingControlRequestRow,
                      floors: dict[str, Decimal] | None) -> tuple[TransitionResult | None, str]:
        now = self.clock()
        if row.action in {"pause", "kill"}:
            # A stop never waits on which build is running, nor on the venue.
            state = REDUCING if row.action == "pause" else HALTED
            result = await append_transition(
                session, account_id=self.account_id, environment=self.environment, state=state,
                cause=CAUSE_OPERATOR, actor=row.requested_by, reason=f"{row.action}: {row.reason}",
                now_ms=now)
            if row.action == "kill":
                # Whatever else was waiting was asked before the stop; none of
                # it may undo the stop after it (a queued resume above all).
                superseded = (await session.execute(
                    update(TradingControlRequestRow).where(
                        TradingControlRequestRow.exchange_account_id == self.account_id,
                        TradingControlRequestRow.deployment_environment == self.environment,
                        TradingControlRequestRow.state == REQUESTED,
                        TradingControlRequestRow.request_id != row.request_id,
                    ).values(state=REJECTED, processed_at_ms=now, outcome_reason="superseded_by_kill")
                    .returning(TradingControlRequestRow.request_id))).scalars().all()
                if superseded:
                    log.warning("trading_control_superseded_by_kill kill=%s superseded=%s",
                                row.request_id, [str(r) for r in superseded])
                return result, "HALTED; venue cancel-all follows (funding_cancel_all_audit)"
            return result, "paused" if result.changed else "already paused"
        if row.backend_digest != self.identity.backend_digest:
            # The operator approves the build they saw; it must be the one running.
            raise RequestRejected("digest_not_running")
        klass, _why = await effective_change_class(session, self.identity, account_id=self.account_id,
                                                   environment=self.environment)
        approved = await is_approved(session, account_id=self.account_id,
                                     environment=self.environment, digest=self.identity.backend_digest)
        current = await read_current(session, account_id=self.account_id, environment=self.environment)

        def fresh() -> Probation:
            if floors is None:
                raise NeedsPreparation
            return Probation.starting(multiplier=self._multiplier, started_at_ms=now, floor=floors)

        if row.action == "approve":
            if klass == STANDARD:
                raise RequestRejected("approval_not_required")
            if approved:
                raise RequestRejected("already_approved")
            assert self.identity.backend_digest is not None and self.identity.source_revision is not None
            leaves_gate = current is None or (current.state == REDUCING
                                              and current.cause == CAUSE_MATERIAL_DEPLOY)
            probation = fresh() if leaves_gate else None
            session.add(DeploymentApprovalRow(
                exchange_account_id=self.account_id, deployment_environment=self.environment,
                backend_digest=self.identity.backend_digest,
                source_revision=self.identity.source_revision, approved_by=row.requested_by,
                approved_at_ms=now, request_id=row.request_id))
            await session.flush()
            if current is not None and not leaves_gate:
                return None, f"approved; trading state {current.state} unchanged"
            result = await append_transition(
                session, account_id=self.account_id, environment=self.environment,
                state=ACTIVE, cause=CAUSE_OPERATOR, actor=row.requested_by,
                reason=f"approved {self.identity.backend_digest}: {row.reason}",
                now_ms=now, probation=probation)
            return result, "approved; ACTIVE in probation"
        # resume
        if klass == MATERIAL and not approved:
            raise RequestRejected("approval_required")
        if current is not None and current.state == ACTIVE:
            raise RequestRejected("already_active")
        probation_now: Probation | None
        if current is None or current.cause in {CAUSE_AUTO, CAUSE_MATERIAL_DEPLOY}:
            # After an automatic stop, or with no decision ever recorded: prove
            # the system again from a new probation.
            probation_now = fresh()
        elif (owed := await unfinished_probation(session, account_id=self.account_id,
                                                 environment=self.environment)) is not None:
            # A pause or an operator's own stop does not end a probation (ADR D3:
            # exposure stays within it until it passes); the same limits restart.
            probation_now = owed.restarted(started_at_ms=now)
        elif klass == MATERIAL and not await digest_accepted(
                session, account_id=self.account_id, environment=self.environment,
                digest=self.identity.backend_digest or ""):
            # Approved while stopped: this build has not run its probation yet.
            probation_now = fresh()
        else:
            probation_now = None  # a pause or an operator's stop of a proven build
        result = await append_transition(
            session, account_id=self.account_id, environment=self.environment, state=ACTIVE,
            cause=CAUSE_OPERATOR, actor=row.requested_by, reason=f"resumed: {row.reason}",
            now_ms=now, probation=probation_now)
        return result, ("resumed; ACTIVE in probation" if probation_now else "resumed")

    async def lift_probation_if_passed(self) -> TradingState | None:
        async with self.session_factory.begin() as session:
            await acquire_transaction_lock(session, account_id=str(self.account_id),
                                           deployment_environment=self.environment)
            current = await read_current(session, account_id=self.account_id,
                                         environment=self.environment)
            if current is None or current.state != ACTIVE or current.probation is None:
                return None
            now = self.clock()
            progress = await probation_progress(
                session, account_id=self.account_id, environment=self.environment,
                probation=current.probation, now_ms=now, bake_ms=self._bake_ms,
                bake_acknowledged=self._bake_acknowledged)
            if not progress.passed:
                return None
            started, acknowledged = progress.started_at_ms, progress.acknowledged
            result = await append_transition(
                session, account_id=self.account_id, environment=self.environment, state=ACTIVE,
                cause=CAUSE_AUTO, actor="probation",
                reason=(f"probation passed: {(now - started) // 3_600_000}h, "
                        f"{acknowledged} acknowledged submits, no HALTED"),
                now_ms=now)
        log.warning("probation_lifted state_id=%s acknowledged=%s", result.state.id, acknowledged)
        alerts.emit(PROBATION_LIFTED, level=alerts.INFO, state_id=result.state.id,
                    hours=(now - started) // 3_600_000, acknowledged=acknowledged)
        announce(result)
        return result.state


__all__ = [
    "BAKE_MIN_ACKNOWLEDGED",
    "BAKE_MS",
    "MATERIAL",
    "PROBATION_MULTIPLIER",
    "STANDARD",
    "DeploymentIdentity",
    "GateDecision",
    "ProbationProgress",
    "TradingControlWorker",
    "apply_deploy_gate",
    "digest_accepted",
    "effective_change_class",
    "is_approved",
    "probation_progress",
]

