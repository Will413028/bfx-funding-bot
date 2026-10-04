"""Hard guards — L1, always-on in 4.2.

Order matters: cheap checks first (manual kill > auth health > heartbeat).
Chain short-circuits on first block. Capital limits are the applied
CapitalPolicy's (CapitalPolicyGuard), not a guard here.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.telemetry import HealthStatus, HealthTarget
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    GuardResult,
    WriterLockHandle,
)
from bfx_funding_bot.modules.execution.safety.trading_state import (
    TradingState,
    TradingStateRepository,
)
from bfx_funding_bot.modules.ledger import (
    CapitalAuthority,
    CapitalBlocked,
    Scope,
    UncertaintyReader,
)
from bfx_funding_bot.modules.strategy import DecisionOutcome, DecisionPayload
from bfx_funding_bot.modules.trading import CapitalScope


class _TradingStateReader(Protocol):
    async def current(self) -> TradingState | None: ...


class CapitalPolicyGuard:
    """The applied-policy evaluator is the sole live money authority."""
    name = "capital_policy"

    def __init__(self, *, authority: CapitalAuthority, scope: Scope,
                 clock: Callable[[], int]) -> None:
        self.authority = authority
        self.scope = scope
        self.clock = clock

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(True, self.name)
        try:
            scope = self.scope
            if ctx.account_id != str(scope.exchange_account_id) or not ctx.capital_cell_id:
                raise ValueError("capital_scope_missing")
            view = await self.authority.read(
                CapitalScope(scope.exchange_account_id, scope.deployment_environment,
                             decision.symbol, ctx.capital_cell_id),
                now_ms=self.clock(), session=ctx.command_session)
            if isinstance(view, CapitalBlocked):
                return GuardResult(False, self.name, f"capital_unavailable: {view.reason}")
            amount = decision.offer_amount_usdt
            if amount is None:
                raise ValueError("offer_amount_missing")
            if not amount.is_finite() or amount <= 0 or amount > view.budget.max_new_offer:
                return GuardResult(False, self.name, view.budget.reason or "insufficient_deployable_funds")
            return GuardResult(True, self.name)
        except Exception as exc:
            return GuardResult(False, self.name, f"capital_unavailable: {exc}")


class ManualKillGuard:
    """The trading-state guard: blocks new offers unless trading is ACTIVE.

    Blocks on an automatic protection that has tripped but whose HALTED is not
    yet committed, and on the durable trading state: anything but ACTIVE blocks,
    and so does no recorded decision at all. The break-glass that needs no
    database is stopping the container (runbook), not an environment flag.

    **Fails closed.** An unreadable trading state blocks. A kill switch that
    opens when the database hiccups is not a kill switch. Note this differs
    from NavPeakStore's fail-permissive posture; the two must not be unified.

    ``trading_state=None`` (paper/shadow): only a tripped protection blocks.
    """

    name = "manual_kill"

    def __init__(
        self,
        *,
        trading_state: _TradingStateReader | None = None,
        pending_stop: Callable[[], str | None] | None = None,
    ) -> None:
        self._trading_state = trading_state
        # An automatic protection that has tripped but whose HALTED is not yet
        # committed. It stops new offers from the instant it trips.
        self._pending_stop = pending_stop

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        pending = self._pending_stop() if self._pending_stop is not None else None
        if pending is not None:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=f"automatic protection tripped, HALTED pending: {pending}",
            )
        if self._trading_state is None:
            return GuardResult(allowed=True, guard_name=self.name)
        try:
            if ctx.command_session is not None:
                if not isinstance(self._trading_state, TradingStateRepository):
                    raise ValueError("same-session trading state reader required")
                state = await self._trading_state.current(ctx.command_session)
            else:
                state = await self._trading_state.current()
        except Exception as exc:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=f"trading state unreadable — failing closed: {exc!r}",
            )
        # None = no decision was ever recorded for this realm: fail closed,
        # exactly like HALTED. A new scope trades only after an operator's
        # explicit ACTIVE.
        if state is None:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="no trading state recorded — treated as HALTED",
            )
        if not state.allows_new_offers:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=(
                    f"trading state {state.state}: {state.reason} "
                    f"(cause={state.cause}, actor={state.actor}, id={state.id})"
                ),
            )
        return GuardResult(allowed=True, guard_name=self.name)


class AuthHealthGuard:
    """Block when executor target probe state is DOWN.

    Allows when target never set (day-1 boot) or HEALTHY/DEGRADED.
    DEGRADED is a soft warn, not a block — chain still considers each request.
    """

    name = "auth_health"

    def __init__(self, *, probe: HealthProbe) -> None:
        self.probe = probe

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        status = self.probe.current_status(HealthTarget.EXECUTOR)
        if status == HealthStatus.DOWN:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="executor health DOWN",
            )
        return GuardResult(allowed=True, guard_name=self.name)


class HeartbeatGuard:
    """Block when any watched sub-task heartbeat is older than threshold.

    Day-1 / never-recorded sub-tasks allow by default (booting state).
    Threshold strictly greater (> threshold) blocks; exactly equal allows.
    """

    name = "heartbeat"

    def __init__(
        self, *, probe: HealthProbe, threshold_seconds: int,
        watched_sub_tasks: list[str],
    ) -> None:
        self.probe = probe
        self.threshold_seconds = threshold_seconds
        self.watched = watched_sub_tasks

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        now = datetime.now(UTC)
        for sub_task in self.watched:
            last = self.probe.last_active_ts.get(sub_task)
            if last is None:
                continue
            # Truncate to integer seconds so "exactly at threshold" semantics
            # are deterministic — microsecond drift from datetime.now() between
            # heartbeat record and evaluate must not flip the boundary case.
            age = int((now - last).total_seconds())
            if age > self.threshold_seconds:
                return GuardResult(
                    allowed=False, guard_name=self.name,
                    reason=f"sub_task={sub_task} stale {age}s > {self.threshold_seconds}s",
                )
        return GuardResult(allowed=True, guard_name=self.name)


class UncertaintyGuard:
    """Fail-closed hard guard for one exact account/environment/symbol scope.

    Unknown future uncertainty kinds are deliberately treated as blocking.  A
    reader failure is also a block; the chain can then emit its normal bounded
    safety trigger without allowing a submit through a missing projection.
    """

    name = "uncertainty"
    _SUPPORTED_KINDS = frozenset({
        "submit_outcome_unknown",
        "unattributed_venue_offer",
        "unsupported_venue_exposure",
    })

    def __init__(
        self,
        *,
        reader: UncertaintyReader,
        deployment_environment: str,
    ) -> None:
        if not deployment_environment.strip():
            raise ValueError("deployment_environment must be non-empty")
        self._reader = reader
        self._deployment_environment = deployment_environment

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(allowed=True, guard_name=self.name)
        try:
            account_id = UUID(str(ctx.account_id))
        except (AttributeError, TypeError, ValueError):
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason="account identity is not canonical — uncertainty guard blocked",
            )
        try:
            rows: Sequence[object] = await self._reader.list_open(
                ctx.command_session,
                Scope(account_id, self._deployment_environment),
                decision.symbol,
            )
        except Exception as exc:
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason=f"uncertainty projection unreadable — failing closed: {exc!r}",
            )
        if rows is None:
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason="uncertainty projection returned no result — failing closed",
            )
        for row in rows:
            kind = row.get("kind") if isinstance(row, dict) else getattr(row, "kind", None)
            row_account = (
                row.get("exchange_account_id", account_id)
                if isinstance(row, dict)
                else getattr(row, "exchange_account_id", account_id)
            )
            row_environment = (
                row.get("deployment_environment", self._deployment_environment)
                if isinstance(row, dict)
                else getattr(row, "deployment_environment", self._deployment_environment)
            )
            row_symbol = (
                row.get("symbol", decision.symbol)
                if isinstance(row, dict)
                else getattr(row, "symbol", decision.symbol)
            )
            if (
                str(row_account) != str(account_id)
                or str(row_environment) != self._deployment_environment
                or str(row_symbol) != decision.symbol
            ):
                # The database adapter already applies this exact predicate;
                # retain the skip here as a defence against a stale/overbroad
                # adapter so one symbol can never stop an unrelated symbol.
                continue
            if kind not in self._SUPPORTED_KINDS:
                return GuardResult(
                    allowed=False,
                    guard_name=self.name,
                    reason=f"unsupported uncertainty kind {kind!r} — failing closed",
                )
            return GuardResult(
                allowed=False,
                guard_name=self.name,
                reason=f"open execution uncertainty kind={kind} symbol={decision.symbol}",
            )
        return GuardResult(allowed=True, guard_name=self.name)


ResolutionSource = Literal["symbol_map", "env_fallback", "default"]


@dataclass(frozen=True, slots=True)
class ResolvedValue:
    """A resolved per-symbol scalar plus WHICH TIER produced it."""
    value: Decimal
    source: ResolutionSource


def resolve_for_symbol_with_source(
    mapping: dict[str, Decimal],
    symbol: str,
    *,
    env_fallback: Decimal | None,
    default: Decimal,
) -> ResolvedValue:
    """Three-tier resolution, reporting the tier that bound.

    Same chain as :func:`resolve_for_symbol` — this is the implementation, and
    the scalar helper delegates here, so the reported source can never describe
    a tier the guards did not actually take.

    Reporting the tier is not cosmetic. On 2026-07-27 the canary was "paused"
    by setting the env scalar to 0; every configured symbol had an explicit
    ``mapping`` entry, so the scalar bound nothing and lending continued for
    hours. The value alone (0) looked like a halt; only the source
    (``symbol_map``, not ``env_fallback``) shows the knob was inert.

    Note the ``is not None`` test on env_fallback: ``Decimal("0")`` is falsy,
    and a truthiness check would silently report ``default`` for exactly the
    value that caused the incident.
    """
    v = mapping.get(symbol)
    if v is not None:
        return ResolvedValue(value=v, source="symbol_map")
    if env_fallback is not None:
        return ResolvedValue(value=env_fallback, source="env_fallback")
    return ResolvedValue(value=default, source="default")


class WriterLockGuard:
    """Fail-closed single-writer guard. Refuses every real-money submit unless
    this process still holds the Postgres advisory writer lock, verified LIVE
    against the dedicated connection (no stale-flag window)."""

    name = "writer_lock"

    def __init__(self, *, lock: WriterLockHandle) -> None:
        self._lock = lock

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if await self._lock.verify_held():
            return GuardResult(allowed=True, guard_name=self.name)
        return GuardResult(
            allowed=False,
            guard_name=self.name,
            reason="writer advisory lock not held — failing closed (refusing real-money submit)",
        )
