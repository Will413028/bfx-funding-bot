"""Release flow by change class: the boot gate, operator requests, probation.

ADR 2026-09-25 D1-D4, plan §1 "Release flow":

- **Boot gate.** The deploy tool injects ``BFX_CHANGE_CLASS``,
  ``BFX_IMAGE_DIGEST`` and ``BFX_SOURCE_REVISION``. A *standard* build keeps
  the trading state it found. A *material* build whose digest has no approval
  parks the writer in ``REDUCING`` (cause ``material_deploy``). No or malformed
  deploy identity (a local run, an old deploy path) is material -- fail closed.
  The ``deployments`` ledger can only raise the class: the deploy tool appends
  its row after the bot is healthy, so the running digest usually has none yet,
  but any row for it saying material, or naming another revision, wins.
- **Requests.** The web API only inserts an approve/resume request; the
  :class:`TradingControlWorker` applies it under the account lock after
  re-checking the operator, and records one outcome on the row.
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
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.external.bitfinex.funding_rules import FundingRuleProvider, submit_amount
from bfx_funding_bot.modules.deployments.tables import DeploymentRow
from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    REJECTED,
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

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DeploymentIdentity:
        digest = (env.get("BFX_IMAGE_DIGEST") or "").strip()
        revision = (env.get("BFX_SOURCE_REVISION") or "").strip()
        klass = (env.get("BFX_CHANGE_CLASS") or "").strip()
        if not (digest or revision or klass):
            return cls(None, None, MATERIAL, "deployment_identity_missing")
        if not _DIGEST.fullmatch(digest) or not _REVISION.fullmatch(revision):
            return cls(digest if _DIGEST.fullmatch(digest) else None,
                       revision if _REVISION.fullmatch(revision) else None,
                       MATERIAL, "deployment_identity_invalid")
        if klass not in {STANDARD, MATERIAL}:
            return cls(digest, revision, MATERIAL, "change_class_invalid")
        return cls(digest, revision, klass)


async def effective_change_class(session: AsyncSession,
                                 identity: DeploymentIdentity) -> tuple[str, str]:
    """The class this build runs under, and why. The ledger can only raise it."""
    if identity.problem is not None or identity.backend_digest is None:
        return MATERIAL, identity.problem or "deployment_identity_missing"
    rows = (await session.scalars(select(DeploymentRow).where(
        DeploymentRow.backend_digest == identity.backend_digest))).all()
    if any(row.source_revision != identity.source_revision for row in rows):
        return MATERIAL, "ledger_revision_conflict"
    if any(row.change_class == MATERIAL for row in rows):
        return MATERIAL, "ledger_material"
    return identity.change_class, "deploy_env"


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
        klass, why = await effective_change_class(session, identity)
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


class TradingControlWorker(OperatorRequestWorker[TradingControlRequestRow, dict[str, Decimal]]):
    """Applies approve/resume requests and lifts a probation that has passed."""

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

    def committed(self, row: TradingControlRequestRow, outcome: Outcome) -> None:
        """Tell the operator; alerting never blocks or raises."""
        fields = {"request_id": str(row.request_id), "action": row.action, "by": row.requested_by}
        if outcome.state == APPLIED:
            result = outcome.detail if isinstance(outcome.detail, TransitionResult) else None
            alerts.emit(TRADING_CONTROL_APPLIED, level=alerts.WARNING, outcome=outcome.reason,
                        state_id=result.state.id if result is not None else "unchanged", **fields)
            if result is not None:
                announce(result)
        elif outcome.state == REJECTED:
            alerts.emit(TRADING_CONTROL_REJECTED, level=alerts.WARNING, reason=outcome.reason,
                        **fields)
        else:
            alerts.emit(TRADING_CONTROL_FAILED, level=alerts.CRITICAL, reason=outcome.reason,
                        **fields)

    async def _decide(self, session: AsyncSession, row: TradingControlRequestRow,
                      floors: dict[str, Decimal] | None) -> tuple[TransitionResult | None, str]:
        if row.backend_digest != self.identity.backend_digest:
            # The operator approves the build they saw; it must be the one running.
            raise RequestRejected("digest_not_running")
        klass, _why = await effective_change_class(session, self.identity)
        approved = await is_approved(session, account_id=self.account_id,
                                     environment=self.environment, digest=self.identity.backend_digest)
        current = await read_current(session, account_id=self.account_id, environment=self.environment)
        now = self.clock()

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
        elif klass == MATERIAL and not await self._build_passed_probation(session):
            # Approved while stopped: this build has not run its probation yet.
            probation_now = fresh()
        else:
            probation_now = None  # a pause or an operator's stop of a proven build
        result = await append_transition(
            session, account_id=self.account_id, environment=self.environment, state=ACTIVE,
            cause=CAUSE_OPERATOR, actor=row.requested_by, reason=f"resumed: {row.reason}",
            now_ms=now, probation=probation_now)
        return result, ("resumed; ACTIVE in probation" if probation_now else "resumed")

    async def _build_passed_probation(self, session: AsyncSession) -> bool:
        """Whether a probation was lifted after this build's approval."""
        approved_at = await session.scalar(select(DeploymentApprovalRow.approved_at_ms).where(
            DeploymentApprovalRow.exchange_account_id == self.account_id,
            DeploymentApprovalRow.deployment_environment == self.environment,
            DeploymentApprovalRow.backend_digest == self.identity.backend_digest,
        ))
        if approved_at is None:
            return False
        return await session.scalar(select(TradingStateRow.id).where(
            TradingStateRow.exchange_account_id == self.account_id,
            TradingStateRow.deployment_environment == self.environment,
            TradingStateRow.state == ACTIVE, TradingStateRow.cause == CAUSE_AUTO,
            TradingStateRow.probation_multiplier.is_(None),
            TradingStateRow.created_at_ms >= approved_at,
        ).limit(1)) is not None

    async def lift_probation_if_passed(self) -> TradingState | None:
        async with self.session_factory.begin() as session:
            await acquire_transaction_lock(session, account_id=str(self.account_id),
                                           deployment_environment=self.environment)
            current = await read_current(session, account_id=self.account_id,
                                         environment=self.environment)
            if current is None or current.state != ACTIVE or current.probation is None:
                return None
            started = current.probation.started_at_ms
            now = self.clock()
            if now - started < self._bake_ms:
                return None
            acknowledged = int(await session.scalar(select(func.count()).select_from(
                SubmissionAttemptRow).where(
                    SubmissionAttemptRow.exchange_account_id == self.account_id,
                    SubmissionAttemptRow.deployment_environment == self.environment,
                    SubmissionAttemptRow.outcome_kind == "acknowledged",
                    SubmissionAttemptRow.started_at_ms >= started,
                )) or 0)
            if acknowledged < self._bake_acknowledged:
                return None
            # Any HALTED since the probation started would have superseded it:
            # the current row still carrying it proves none happened.
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
    "TradingControlWorker",
    "apply_deploy_gate",
    "effective_change_class",
    "is_approved",
]

