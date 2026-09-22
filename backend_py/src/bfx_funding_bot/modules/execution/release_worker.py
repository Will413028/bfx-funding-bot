"""Release authority attached to the existing daemon's sole command gate."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.release_identity import ReleaseRuntime, assert_protected_file
from bfx_funding_bot.core.settings import AuthSettings
from bfx_funding_bot.core.writer_lock import WriterLock
from bfx_funding_bot.external.bitfinex.funding_rules import (
    RULE,
    FundingAmountEvidence,
    FundingRuleProvider,
    minimum_amount,
    validate_amount,
)
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit
from bfx_funding_bot.modules.execution.event_store.writer import DEFAULT_PROJECTOR_VERSION
from bfx_funding_bot.modules.execution.protocols import AccountContext
from bfx_funding_bot.modules.execution.release_session import (
    ReleaseBlocked,
    ReleaseCommand,
    ReleaseSessions,
)
from bfx_funding_bot.modules.execution.release_tables import ReleaseSessionRow
from bfx_funding_bot.modules.execution.safety.halt_state import HaltStateStore
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    CanaryCommandPermitRow,
    SubmissionAttemptRow,
)

log = logging.getLogger(__name__)

RELEASE_SCHEMA_HEAD = "e5c9a3f10b62"


def build_release_worker(*, runtime: ReleaseRuntime, capital: CapitalRuntime,
                         writer_lock: WriterLock, halt_store: HaltStateStore,
                         funding_rules: FundingRuleProvider | None, configured_cells: tuple[tuple[str, str, str], ...],
                         halt_authorization: object, planner: Callable[[ReleaseCommand], Awaitable[None]],
                         config_artifact: Path, evidence_path: Path,
                         clock: Callable[[], int]) -> ReleaseWorker:
    """Wire measured authority and existing Halt2 verifier, never a second executor."""
    from bfx_funding_bot.modules.marketfeed.daemon import CanaryProfile
    from scripts.halt2_cutover import _load_evidence
    from scripts.run_canary_preflight import CanaryAttemptSelector, verify_release_preflight

    repo = ReleaseSessions(capital.repository.account_id, capital.repository.environment)

    async def binding_reader(session: AsyncSession) -> dict[str, Any]:
        proof = await asyncio.to_thread(runtime.verify)
        heads = tuple(await session.scalars(text("SELECT version_num FROM alembic_version")))
        if (heads != (RELEASE_SCHEMA_HEAD,) or proof.manifest.schema_head != RELEASE_SCHEMA_HEAD
            or proof.manifest.projector_version != DEFAULT_PROJECTOR_VERSION):
            raise ReleaseBlocked("release_schema_or_projector_mismatch")
        policies = {}
        for symbol in ("fUST", "fUSD"):
            applied = await capital.repository.read_applied(session, symbol=symbol)
            if applied.policy.enabled != (symbol == "fUST"):
                raise ReleaseBlocked("release_currency_policy_mismatch")
            policies[symbol] = {"revision": applied.revision, "digest": applied.digest}
        return {"release_digest": proof.release_digest, "config_digest": proof.config_digest,
                "source_revision": proof.manifest.source_revision,
                "schema_head": RELEASE_SCHEMA_HEAD, "projector_version": DEFAULT_PROJECTOR_VERSION,
                "policies": policies, "funding_rule_digest": RULE.digest}

    async def operator(session: AsyncSession, user: str) -> bool:
        settings = AuthSettings()
        if not user or user != settings.operator_user_id or settings.operator_role != "admin":
            return False
        return bool(await session.scalar(text("SELECT public.release_operator_authorized(:account, :actor)"),
                                         {"account": repo.account_id, "actor": user}))

    async def readiness(session: AsyncSession, row: ReleaseSessionRow, *, observe: bool) -> object:
        proof = await asyncio.to_thread(runtime.verify)
        assert_protected_file(evidence_path)
        max_age = int(proof.manifest.environment.get("BFX_HALT2_MAX_SNAPSHOT_AGE_SECONDS", "300"))
        if not 1 <= max_age <= 300:
            raise ReleaseBlocked("release_snapshot_age_out_of_bounds")
        if row.minimum_amount is None:
            raise ReleaseBlocked("session_minimum_missing")
        profile = CanaryProfile(account_id=repo.account_id, environment=repo.environment,
            symbol=row.symbol, cell=row.cell, strategy=row.strategy,
            amount_usdt=Decimal(row.minimum_amount), cap_usdt=Decimal(row.max_amount), max_evidence_age_seconds=max_age)
        selector = None
        if observe:
            if row.permit_id is None or row.attempt_id is None or row.decision_id is None:
                raise ReleaseBlocked("session_outcome_missing")
            selector = CanaryAttemptSelector(permit_id=str(row.permit_id), attempt_id=str(row.attempt_id),
                                              command_decision_id=row.decision_id)
        return await verify_release_preflight(session=session, profile=profile,
            halt2_evidence=_load_evidence(Path(evidence_path)), config_artifact=config_artifact,
            image_digest=proof.actual_image_id, projector_version=DEFAULT_PROJECTOR_VERSION,
            now_ms=clock(), selector=selector)

    async def preflight(session: AsyncSession, row: ReleaseSessionRow) -> object:
        return await readiness(session, row, observe=False)

    async def observation(session: AsyncSession, row: ReleaseSessionRow) -> object:
        return await readiness(session, row, observe=True)

    authority = ReleaseCommandAuthority(repo=repo, capital=capital, binding_reader=binding_reader,
        ownership=writer_lock.verify_held, authority_reader=operator, preflight=preflight, clock=clock)
    return ReleaseWorker(authority=authority, halt_store=halt_store, funding_rules=funding_rules,
        configured_cells=configured_cells, halt_authorization=halt_authorization,
        planner=planner, observation=observation)


class ReleaseCommandAuthority:
    def __init__(self, *, repo: ReleaseSessions, capital: CapitalRuntime,
                 binding_reader: Callable[[AsyncSession], Awaitable[dict[str, Any]]],
                 ownership: Callable[[], Awaitable[bool]],
                 authority_reader: Callable[[AsyncSession, str], Awaitable[bool]],
                 preflight: Callable[[AsyncSession, ReleaseSessionRow], Awaitable[object]],
                 clock: Callable[[], int]) -> None:
        self.repo, self.capital = repo, capital
        self.binding_reader, self.ownership = binding_reader, ownership
        self.authority_reader, self.preflight, self.clock = authority_reader, preflight, clock

    async def binding(self, session: AsyncSession) -> dict[str, Any]:
        if not await self.ownership():
            raise ReleaseBlocked("release_writer_ownership_lost")
        return await self.binding_reader(session)

    async def check_normal(self, session: AsyncSession) -> None:
        """Normal lending needs a promotion for *this build*, not for this pause.

        The canary proves one thing: that this exact artifact, config, policy,
        schema and projector can place a real offer. `binding` already names all
        of those, so comparing it is the whole test. Matching the promotion to
        the current `halt.id` as well tied the proof to a pause count instead --
        every maintenance stop advanced the id and retired a promotion that
        nothing had invalidated, so undoing a database upgrade demanded a fresh
        real-money submit. The promotion is retained across halt cycles and
        retired by a changed binding, which is what actually changes the risk.
        """
        binding = await self.binding(session)
        halt = await self.repo.halt(session)
        promoted = await session.scalar(
            select(ReleaseSessionRow)
            .where(
                ReleaseSessionRow.exchange_account_id == self.repo.account_id,
                ReleaseSessionRow.deployment_environment == self.repo.environment,
                ReleaseSessionRow.state == "promoted",
            )
            .order_by(ReleaseSessionRow.promoted_halt_id.desc())
            .limit(1)
        )
        if halt.halted or promoted is None or promoted.binding != binding:
            raise ReleaseBlocked("release_promotion_required")

    async def admit(self, session: AsyncSession, *, ready: ReadyToSubmit,
                    context: AccountContext, attempt_id: UUID) -> None:
        if context.release_session_id is None:
            await self.check_normal(session)
            return
        binding = await self.binding(session)
        row = await self.repo.get(session, context.release_session_id)
        if row.state != "authorized":
            raise ReleaseBlocked("permit_already_consumed")
        if row.authorized_by is None or not await self.authority_reader(session, row.authorized_by):
            raise ReleaseBlocked("release_operator_revoked")
        await self.preflight(session, row)
        decision = await session.get(ExecutionDecisionRow, ready.decision_id)
        if decision is None:
            raise ReleaseBlocked("session_decision_missing")
        await self.repo.consume(session, row.id, binding=binding, decision=decision,
                                attempt_id=attempt_id, now_ms=self.clock())

    async def before_transport(self, ready: ReadyToSubmit, context: AccountContext) -> None:
        async with self.capital.session_factory() as session:
            await self.repo.lock(session)
            if context.release_session_id is None:
                await self.check_normal(session)
                return
            row = await self.repo.get(session, context.release_session_id)
            binding = await self.binding(session)
            await self.repo.check_current(session, row, binding, now_ms=self.clock(), submit=True)
            if row.state != "consumed" or row.decision_id != ready.decision_id:
                raise ReleaseBlocked("session_consumed_decision_mismatch")


class ReleaseHaltError(RuntimeError):
    """Fatal persistence failure; must escape session-level error handling."""


class ReleaseWorker:
    """Session-row handoff, supervised inside the existing account daemon."""
    def __init__(self, *, authority: ReleaseCommandAuthority, halt_store: HaltStateStore,
                 funding_rules: FundingRuleProvider | None, configured_cells: tuple[tuple[str, str, str], ...],
                 halt_authorization: object, planner: Callable[[ReleaseCommand], Awaitable[None]],
                 observation: Callable[[AsyncSession, ReleaseSessionRow], Awaitable[object]]) -> None:
        self.authority, self.halt_store = authority, halt_store
        self.funding_rules, self.configured_cells = funding_rules, configured_cells
        self.halt_authorization, self.planner, self.observation = halt_authorization, planner, observation

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)

    async def _reassert_halt(self, reason: str) -> None:
        try:
            await self.halt_store.set_halted(True, reason=reason, actor="worker")
        except Exception as exc:
            raise ReleaseHaltError("release_halt_persistence_failed") from exc

    async def tick(self) -> None:
        repo = self.authority.repo
        factory = self.authority.capital.session_factory
        session_id = None
        command = None
        try:
            async with factory.begin() as session:
                await repo.lock(session)
                row = await session.scalar(select(ReleaseSessionRow).where(
                    ReleaseSessionRow.exchange_account_id == repo.account_id,
                    ReleaseSessionRow.deployment_environment == repo.environment,
                    or_(ReleaseSessionRow.request_revision > ReleaseSessionRow.processed_revision,
                        ReleaseSessionRow.state.in_(("authorized", "consumed"))),
                ).order_by(ReleaseSessionRow.created_at_ms, ReleaseSessionRow.id).limit(1))
                if row is None:
                    halt = await repo.halt(session)
                    if not halt.halted:
                        await self.authority.check_normal(session)
                    return
                session_id = row.id
                symbol = row.symbol
                # Observe FX for whichever transition sets the amount: preparing the
                # session, and again when the human authorises it. The venue rule is
                # USD-denominated, so its UST equivalent drifts; deriving at the click
                # rather than at the preview is what keeps the gap to submission down
                # to seconds instead of however long someone took to read the screen.
                needs_amount = row.state == "requested" or (
                    row.request_revision > row.processed_revision
                    and row.requested_action == "authorize"
                )
            # Public FX observation outside any DB transaction. Reacquire the
            # same account lock and reload the row before applying authority.
            amount_evidence = None
            if needs_amount:
                if self.funding_rules is None:
                    raise ReleaseBlocked("funding_rule_unavailable")
                amount_evidence = await self.funding_rules.observe(symbol)
            async with factory.begin() as session:
                await repo.lock(session)
                row = await repo.get(session, session_id)
                command = await self._apply(session, row, amount_evidence=amount_evidence)
            if command is not None:
                try:
                    await self.planner(command)
                finally:
                    # Failure is fatal: it propagates through TaskGroup and stops
                    # the entire writer, rather than being a best-effort log.
                    await self._reassert_halt("release_command_terminal")
        except ReleaseHaltError:
            raise
        except Exception as exc:
            # ``reason`` below stays a bounded code, so an unexpected exception
            # would otherwise leave no readable trace anywhere. Log it here,
            # before halt persistence can raise over it.
            log.exception("release_session_blocked session_id=%s", session_id)
            await self._reassert_halt("release_blocked")
            if session_id is None:
                # Runtime/proof failure with an unhalted account must stop the
                # writer after persisting halt. No blanket retry loop.
                raise
            async with factory.begin() as session:
                await repo.lock(session)
                row = await repo.get(session, session_id)
                # Incomplete observation can be requested again without any new
                # submit. Authorization/runtime faults terminally block instead.
                from bfx_funding_bot.modules.marketfeed.daemon import CanaryStartupBlocked
                if not (isinstance(exc, CanaryStartupBlocked) and row.state in {"consumed", "observed"}):
                    row.state = "blocked"
                row.reason = str(exc) if isinstance(exc, (ReleaseBlocked, CanaryStartupBlocked)) else type(exc).__name__
                row.processed_revision = row.request_revision
                repo.audit(session, row, action="blocked", actor="worker", now_ms=self.authority.clock(),
                           evidence={"reason": row.reason})

    async def _apply(self, session: AsyncSession, row: ReleaseSessionRow, *,
                     amount_evidence: FundingAmountEvidence | None = None) -> ReleaseCommand | None:
        repo, now = self.authority.repo, self.authority.clock()
        binding = await self.authority.binding(session)
        pending = row.request_revision > row.processed_revision
        if pending and not await self.authority.authority_reader(session, row.requested_by):
            raise ReleaseBlocked("release_operator_revoked")
        if row.state == "requested":
            if self.configured_cells.count((row.strategy, row.symbol, row.cell)) != 1:
                raise ReleaseBlocked("session_cell_not_configured")
            view = await self.authority.capital.read(symbol=row.symbol, cell_id=row.cell, session=session)
            minimum = minimum_amount(amount_evidence, symbol=row.symbol, now_ms=self.authority.clock())
            if view.budget.max_new_offer < minimum:
                raise ReleaseBlocked("session_insufficient_capital")
            if minimum > row.max_amount:
                raise ReleaseBlocked("session_minimum_or_expiry")
            # Readiness validates the exact locally inferred preview amount.
            # Any failure rolls this transaction back before recording preview.
            row.minimum_amount = minimum
            await self.authority.preflight(session, row)
            halt = await repo.halt(session)
            validate_amount(minimum, amount_evidence, symbol=row.symbol, now_ms=self.authority.clock())
            row = await repo.prepare(session, row.id, binding=binding, halt_id=halt.id,
                               minimum_amount=minimum, now_ms=self.authority.clock())
            row.evidence = {"preparation": {
                "available_amount": str(view.snapshot.available_amount),
                "max_new_offer": str(view.budget.max_new_offer),
                "minimum_amount": str(minimum),
                "funding_amount": amount_evidence.payload() if amount_evidence is not None else None,
                "snapshot_seq": view.snapshot_seq,
                "policy_revision": view.applied.revision,
            }}
            repo.audit(session, row, action="prepared_capital", actor="worker", now_ms=now,
                       evidence=row.evidence)
            return None
        if pending and row.requested_action == "authorize":
            await self.authority.preflight(session, row)
            row = await repo.authorize(session, row.id, binding=binding, now_ms=now)
            # Re-derive against FX observed for this authorisation, not the preview
            # taken when the session was prepared. The preview is what the operator
            # saw; this is what the rule requires at the moment they committed.
            minimum = minimum_amount(amount_evidence, symbol=row.symbol,
                                     now_ms=self.authority.clock())
            view = await self.authority.capital.read(symbol=row.symbol, cell_id=row.cell,
                                                     session=session)
            if view.budget.max_new_offer < minimum:
                raise ReleaseBlocked("session_insufficient_capital")
            if minimum > row.max_amount:
                raise ReleaseBlocked("session_minimum_or_expiry")
            validate_amount(minimum, amount_evidence, symbol=row.symbol,
                            now_ms=self.authority.clock())
            row.minimum_amount = minimum
        if row.state == "authorized":
            await repo.check_current(session, row, binding, now_ms=now, submit=True)
            if row.minimum_amount is None:
                raise ReleaseBlocked("session_minimum_missing")
            return ReleaseCommand(row.id, row.symbol, row.cell, row.strategy,
                                  row.minimum_amount, row.max_amount,
                                  self.halt_authorization)
        if row.state == "consumed":
            attempt = await session.get(SubmissionAttemptRow, row.attempt_id)
            if attempt is None or attempt.completed_at_ms is None:
                return None
            if (attempt.execution_decision_id != row.decision_id
                or attempt.exchange_account_id != repo.account_id
                or attempt.deployment_environment != repo.environment
                or attempt.symbol != row.symbol or attempt.outcome_kind != "acknowledged"
                or not attempt.venue_offer_id or attempt.last_event_seq is None):
                raise ReleaseBlocked("session_outcome_not_acknowledged")
            permit = await session.get(CanaryCommandPermitRow, row.permit_id)
            if permit is None:
                raise ReleaseBlocked("session_permit_missing")
            permit.attempt_id = attempt.attempt_id
            row.state = "observed"
            repo.audit(session, row, action="observed", actor="worker", now_ms=now,
                       evidence={"attempt_id": str(attempt.attempt_id), "venue_offer_id": attempt.venue_offer_id})
            await session.flush()
        if pending and row.requested_action in {"validate", "promote"}:
            await repo.check_current(session, row, binding, now_ms=now, submit=False)
            from bfx_funding_bot.modules.marketfeed.daemon import CanaryEvidence
            proof = await self.observation(session, row)
            if not isinstance(proof, CanaryEvidence) or (
                proof.attempt_id != str(row.attempt_id) or proof.command_decision_id != row.decision_id
                or proof.permit_id != str(row.permit_id)):
                raise ReleaseBlocked("session_validation_proof_mismatch")
            # Readiness/replay can take time. Re-measure authority after it, not
            # just when the delayed human request was first selected.
            await repo.check_current(session, row, await self.authority.binding(session),
                                     now_ms=self.authority.clock(), submit=False)
            if not await self.authority.authority_reader(session, row.requested_by):
                raise ReleaseBlocked("release_operator_revoked")
            row.evidence = json.loads(json.dumps(asdict(proof), default=str))
            if row.requested_action == "validate" and row.state == "observed":
                row.state = "validated"
            elif row.requested_action == "promote" and row.state == "validated":
                # Auth and current binding have been rechecked in this same
                # transaction, after acquiring the account lock. No static token.
                transition = TradingHaltRow(account_id=str(repo.account_id), exchange_account_id=repo.account_id,
                    deployment_environment=repo.environment, halted=False,
                    actor=row.requested_by, reason="release_promoted:" + str(row.id), created_at_ms=now)
                session.add(transition)
                await session.flush()
                row.state, row.promoted_halt_id = "promoted", transition.id
            else:
                raise ReleaseBlocked("session_transition_conflict")
            row.processed_revision = row.request_revision
            row.reason = None
            repo.audit(session, row, action=row.state, actor=row.requested_by, now_ms=now, evidence=row.evidence)
        return None
