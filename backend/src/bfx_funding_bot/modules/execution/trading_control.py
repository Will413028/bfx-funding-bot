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
  submits and no HALTED in between (any HALTED supersedes it; the resume that
  follows starts a new one). An operator's REDUCING -> ACTIVE maintenance
  resume does not start one: no new build, nothing went wrong.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.external.bitfinex.funding_rules import FundingRuleProvider, submit_amount
from bfx_funding_bot.modules.deployments.tables import DeploymentRow
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
    REDUCING,
    IllegalTradingTransition,
    Probation,
    TradingState,
    append_transition,
    read_current,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import SubmissionAttemptRow

log = logging.getLogger(__name__)

STANDARD = "standard"
MATERIAL = "material"
PROBATION_MULTIPLIER = Decimal("0.25")
BAKE_MS = 24 * 60 * 60 * 1000
BAKE_MIN_ACKNOWLEDGED = 3

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
            current, action = result.state, "reducing"
    log.warning("deploy_gate class=%s why=%s approved=%s action=%s digest=%s", klass, why,
                approved, action, identity.backend_digest)
    return GateDecision(klass, why, approved, action, current)


class OperatorAuthority(Protocol):
    async def __call__(self, session: AsyncSession, *, account_id: UUID, user: str) -> bool: ...


async def sql_operator_authorized(session: AsyncSession, *, account_id: UUID, user: str) -> bool:
    """The configured operator, an admin with two-factor enabled and a write membership."""
    from bfx_funding_bot.core.settings import AuthSettings

    settings = AuthSettings()
    if not user or user != settings.operator_user_id or settings.operator_role != "admin":
        return False
    return bool(await session.scalar(
        text("SELECT public.trading_operator_authorized(:account, :actor)"),
        {"account": account_id, "actor": user}))


class _Rejected(Exception):  # noqa: N818 - a bounded outcome, not a fault
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TradingControlWorker:
    """Applies operator requests and lifts a probation that has passed."""

    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession], account_id: UUID,
        environment: str, identity: DeploymentIdentity, symbols: Iterable[str],
        funding_rules: FundingRuleProvider | None, authority: OperatorAuthority,
        clock: Callable[[], int] | None = None, interval_s: float = 5.0,
        multiplier: Decimal = PROBATION_MULTIPLIER, bake_ms: int = BAKE_MS,
        bake_acknowledged: int = BAKE_MIN_ACKNOWLEDGED,
    ) -> None:
        self._sf = session_factory
        self.account_id = account_id
        self.environment = environment
        self.identity = identity
        self._symbols = tuple(sorted(set(symbols)))
        self._funding_rules = funding_rules
        self._authority = authority
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._interval_s = interval_s
        self._multiplier = multiplier
        self._bake_ms = bake_ms
        self._bake_acknowledged = bake_acknowledged

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # a control-plane fault must not kill the writer
                log.exception("trading_control_tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)

    async def tick(self) -> None:
        for request_id in await self._pending():
            await self.apply(request_id)
        await self.lift_probation_if_passed()

    async def _pending(self) -> list[UUID]:
        async with self._sf() as session:
            return list(await session.scalars(select(TradingControlRequestRow.request_id).where(
                TradingControlRequestRow.exchange_account_id == self.account_id,
                TradingControlRequestRow.deployment_environment == self.environment,
                TradingControlRequestRow.state == "requested",
            ).order_by(TradingControlRequestRow.created_at_ms).limit(10)))

    async def _floors(self) -> dict[str, Decimal]:
        """One venue-minimum offer per currency, observed now (outside any transaction)."""
        if self._funding_rules is None:
            raise _Rejected("funding_rule_unavailable")
        floors: dict[str, Decimal] = {}
        for symbol in self._symbols:
            evidence = await self._funding_rules.observe(symbol)
            floors[symbol] = submit_amount(evidence, symbol=symbol, now_ms=self._clock())
        return floors

    async def apply(self, request_id: UUID) -> str:
        """Apply one request; returns the recorded outcome state."""
        try:
            floors = await self._floors()
        except _Rejected as exc:
            return await self._record(request_id, "failed", exc.code)
        except Exception as exc:
            return await self._record(request_id, "failed", f"funding_rule_unavailable: {type(exc).__name__}")
        try:
            async with self._sf.begin() as session:
                await acquire_transaction_lock(session, account_id=str(self.account_id),
                                               deployment_environment=self.environment)
                row = await self._load(session, request_id)
                if row is None:
                    return "missing"
                if row.state != "requested":
                    return row.state
                try:
                    state_id, note = await self._decide(session, row, floors)
                except (_Rejected, IllegalTradingTransition) as exc:
                    code = exc.code if isinstance(exc, _Rejected) else f"illegal_transition: {exc}"
                    row.state, row.outcome_reason = "rejected", code[:500]
                    row.processed_at_ms = self._clock()
                    log.warning("trading_control_rejected request=%s action=%s reason=%s",
                                request_id, row.action, code)
                    return "rejected"
                row.state, row.processed_at_ms = "applied", self._clock()
                row.outcome_reason, row.trading_state_id = note, state_id
                log.warning("trading_control_applied request=%s action=%s by=%s state_id=%s note=%s",
                            request_id, row.action, row.requested_by, state_id, note)
                return "applied"
        except Exception as exc:
            log.exception("trading_control_failed request=%s", request_id)
            return await self._record(request_id, "failed", f"apply_failed: {type(exc).__name__}")

    async def _load(self, session: AsyncSession, request_id: UUID) -> TradingControlRequestRow | None:
        row: TradingControlRequestRow | None = await session.scalar(select(TradingControlRequestRow).where(
            TradingControlRequestRow.request_id == request_id,
            TradingControlRequestRow.exchange_account_id == self.account_id,
            TradingControlRequestRow.deployment_environment == self.environment,
        ).execution_options(populate_existing=True))
        return row

    async def _record(self, request_id: UUID, state: str, reason: str) -> str:
        async with self._sf.begin() as session:
            await acquire_transaction_lock(session, account_id=str(self.account_id),
                                           deployment_environment=self.environment)
            row = await self._load(session, request_id)
            if row is None or row.state != "requested":
                return row.state if row is not None else "missing"
            row.state, row.outcome_reason = state, reason[:500]
            row.processed_at_ms = self._clock()
        log.error("trading_control_%s request=%s reason=%s", state, request_id, reason)
        return state

    async def _decide(self, session: AsyncSession, row: TradingControlRequestRow,
                      floors: dict[str, Decimal]) -> tuple[int | None, str | None]:
        if row.backend_digest != self.identity.backend_digest:
            # The operator approves the build they saw; it must be the one running.
            raise _Rejected("digest_not_running")
        if not await self._authority(session, account_id=self.account_id, user=row.requested_by):
            raise _Rejected("operator_not_authorized")
        klass, _why = await effective_change_class(session, self.identity)
        approved = await is_approved(session, account_id=self.account_id,
                                     environment=self.environment, digest=self.identity.backend_digest)
        current = await read_current(session, account_id=self.account_id, environment=self.environment)
        now = self._clock()
        probation = Probation.starting(multiplier=self._multiplier, started_at_ms=now, floor=floors)
        if row.action == "approve":
            if klass == STANDARD:
                raise _Rejected("approval_not_required")
            if approved:
                raise _Rejected("already_approved")
            assert self.identity.backend_digest is not None and self.identity.source_revision is not None
            session.add(DeploymentApprovalRow(
                exchange_account_id=self.account_id, deployment_environment=self.environment,
                backend_digest=self.identity.backend_digest,
                source_revision=self.identity.source_revision, approved_by=row.requested_by,
                approved_at_ms=now, request_id=row.request_id))
            await session.flush()
            if current is None or (current.state == REDUCING and current.cause == CAUSE_MATERIAL_DEPLOY):
                result = await append_transition(
                    session, account_id=self.account_id, environment=self.environment,
                    state=ACTIVE, cause=CAUSE_OPERATOR, actor=row.requested_by,
                    reason=f"approved {self.identity.backend_digest}: {row.reason}",
                    now_ms=now, probation=probation)
                return result.state.id, "approved; ACTIVE in probation"
            return None, f"approved; trading state {current.state} unchanged"
        # resume
        if klass == MATERIAL and not approved:
            raise _Rejected("approval_required")
        if current is not None and current.state == ACTIVE:
            raise _Rejected("already_active")
        if current is not None and current.state == REDUCING and current.cause == CAUSE_OPERATOR:
            probation_now: Probation | None = None  # a maintenance pause: nothing new to prove
        elif (current is None or current.cause in {CAUSE_AUTO, CAUSE_MATERIAL_DEPLOY}
              or (klass == MATERIAL and not await self._build_passed_probation(session))):
            # After an automatic stop, with no decision ever recorded, or with a
            # material build that has not yet run its probation.
            probation_now = probation
        else:
            probation_now = None  # an operator's own stop of a proven build
        result = await append_transition(
            session, account_id=self.account_id, environment=self.environment, state=ACTIVE,
            cause=CAUSE_OPERATOR, actor=row.requested_by, reason=f"resumed: {row.reason}",
            now_ms=now, probation=probation_now)
        return result.state.id, ("resumed; ACTIVE in probation" if probation_now else "resumed")

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
        async with self._sf.begin() as session:
            await acquire_transaction_lock(session, account_id=str(self.account_id),
                                           deployment_environment=self.environment)
            current = await read_current(session, account_id=self.account_id,
                                         environment=self.environment)
            if current is None or current.state != ACTIVE or current.probation is None:
                return None
            started = current.probation.started_at_ms
            now = self._clock()
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
        return result.state


__all__ = [
    "BAKE_MIN_ACKNOWLEDGED",
    "BAKE_MS",
    "MATERIAL",
    "PROBATION_MULTIPLIER",
    "STANDARD",
    "DeploymentIdentity",
    "GateDecision",
    "OperatorAuthority",
    "TradingControlWorker",
    "apply_deploy_gate",
    "effective_change_class",
    "is_approved",
    "sql_operator_authorized",
]

