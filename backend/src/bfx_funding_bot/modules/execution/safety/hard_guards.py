"""Hard guards — L1, always-on in 4.2.

Order matters: cheap checks first (manual kill > auth health > heartbeat >
allocation cap). Chain short-circuits on first block.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.capital_runtime import CapitalRuntime
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    GuardResult,
    WriterLockHandle,
)
from bfx_funding_bot.modules.execution.safety.trading_state import (
    TradingState,
    TradingStateRepository,
)
from bfx_funding_bot.modules.execution.uncertainty_tables import ExecutionUncertaintyRow
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthProbe
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionOutcome,
    DecisionPayload,
    HealthStatus,
    HealthTarget,
)


class _TradingStateReader(Protocol):
    async def current(self) -> TradingState | None: ...


class CapitalPolicyGuard:
    """The applied-policy evaluator is the sole live money authority."""
    name = "capital_policy"
    is_calibrated = False

    def __init__(self, *, runtime: CapitalRuntime) -> None:
        self.runtime = runtime

    async def evaluate(self, decision: DecisionPayload, ctx: AccountContext) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(True, self.name)
        try:
            if ctx.account_id != str(self.runtime.repository.account_id) or not ctx.capital_cell_id:
                raise ValueError("capital_scope_missing")
            view = await self.runtime.read(symbol=decision.symbol, cell_id=ctx.capital_cell_id,
                                           session=ctx.command_session)
            amount = Decimal(str(decision.offer_amount_usdt))
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
    is_calibrated = False

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
    is_calibrated = False

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
    is_calibrated = False

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


class UncertaintyReader(Protocol):
    """Read open uncertainty rows for one exact account/environment/symbol."""

    async def list_open(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> Sequence[object]: ...


class DatabaseUncertaintyReader:
    """Database adapter used by the hard guard and pre-sizing boundary."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_open(
        self,
        *,
        exchange_account_id: UUID,
        deployment_environment: str,
        symbol: str,
    ) -> Sequence[object]:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(ExecutionUncertaintyRow).where(
                        ExecutionUncertaintyRow.exchange_account_id == exchange_account_id,
                        ExecutionUncertaintyRow.deployment_environment == deployment_environment,
                        ExecutionUncertaintyRow.symbol == symbol,
                        ExecutionUncertaintyRow.state == "open",
                    )
                )
            ).scalars().all()


class UncertaintyGuard:
    """Fail-closed hard guard for one exact account/environment/symbol scope.

    Unknown future uncertainty kinds are deliberately treated as blocking.  A
    reader failure is also a block; the chain can then emit its normal bounded
    safety trigger without allowing a submit through a missing projection.
    """

    name = "uncertainty"
    is_calibrated = False
    _SUPPORTED_KINDS = frozenset({
        "submit_outcome_unknown",
        "unattributed_venue_offer",
        "unsupported_venue_exposure",
    })

    def __init__(
        self,
        *,
        reader: UncertaintyReader | Any,
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
            list_open = getattr(self._reader, "list_open", None)
            rows: Sequence[object]
            if ctx.command_session is not None:
                rows = (await ctx.command_session.scalars(
                    select(ExecutionUncertaintyRow).where(
                        ExecutionUncertaintyRow.exchange_account_id == account_id,
                        ExecutionUncertaintyRow.deployment_environment == self._deployment_environment,
                        ExecutionUncertaintyRow.symbol == decision.symbol,
                        ExecutionUncertaintyRow.state == "open",
                    )
                )).all()
            elif list_open is not None:
                rows = await list_open(
                    exchange_account_id=account_id,
                    deployment_environment=self._deployment_environment,
                    symbol=decision.symbol,
                )
            else:
                # Compatibility with the command-gate reader used by older
                # adapters; a true result still blocks the exact scope.
                legacy_reader: Any = self._reader
                has_open = legacy_reader.has_open
                is_open = await has_open(
                    exchange_account_id=account_id,
                    deployment_environment=self._deployment_environment,
                    symbol=decision.symbol,
                )
                rows = ({"kind": "submit_outcome_unknown"},) if is_open else ()
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


def resolve_for_symbol(
    mapping: dict[str, Decimal],
    symbol: str,
    env_fallback: Decimal | None,
    default: Decimal,
) -> Decimal:
    """Three-tier per-symbol value resolution shared by the per-symbol guards.

    1. explicit ``mapping[symbol]`` (the Phase 2 per-symbol config map);
    2. else ``env_fallback`` when provided (the legacy global env scalar);
    3. else ``default``.

    Used for both AllocationCapGuard's cap and BuyingPowerGuard's buffer so the
    fallback chain stays byte-identical across the two guards. Public (Phase 2
    Task 8) because the DeploymentReconciler also resolves caps/buffers with this
    exact chain — the reconciler must size to the SAME cap the guard enforces, or
    sizing and the per-offer guard diverge (silent under/over-deployment).

    Thin wrapper over :func:`resolve_for_symbol_with_source` — one resolution,
    two views. Do NOT reimplement the chain here.
    """
    return resolve_for_symbol_with_source(
        mapping, symbol, env_fallback=env_fallback, default=default,
    ).value


class _LedgerProtocol(Protocol):
    def current_exposure(self, symbol: str) -> Decimal: ...


class AllocationCapGuard:
    """Block POST decision when current_exposure + offer_amount > caps[symbol].

    Phase 2: the cap is PER-SYMBOL. ``caps`` maps symbol → cap (e.g.
    {"fUST": 3000, "fUSD": 0}); a symbol with cap=0 is dark (every POST
    blocked). A symbol absent from ``caps`` falls back to ``env_fallback_cap``
    (the legacy global BFX_ALLOCATION_CAP_USDT env value) when provided, else to
    ``default_cap``. Exposure is read per-symbol so currency buckets are isolated.

    SKIP decisions always allowed (no cap consumption). Exactly-at-cap
    allows; strictly over blocks (so cap=500, exposure=400, offer=100 → allowed
    at 500 = cap; cap=500, exposure=400, offer=101 → blocked at 501 > 500).
    """

    name = "allocation_cap"
    is_calibrated = False

    def __init__(
        self,
        *,
        ledger: _LedgerProtocol,
        caps: dict[str, Decimal],
        default_cap: Decimal,
        env_fallback_cap: Decimal | None = None,
    ) -> None:
        self.ledger = ledger
        self._caps = caps
        self._default_cap = default_cap
        self._env_fallback = env_fallback_cap

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(allowed=True, guard_name=self.name)
        if decision.offer_amount_usdt is None:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="POST decision missing offer_amount_usdt",
            )
        cap = resolve_for_symbol(
            self._caps, decision.symbol, self._env_fallback, self._default_cap,
        )
        exposure = self.ledger.current_exposure(decision.symbol)
        offer = Decimal(str(decision.offer_amount_usdt))
        projected = exposure + offer
        if projected > cap:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=(
                    f"symbol={decision.symbol} exposure={exposure}+offer={offer}"
                    f" → {projected} > cap={cap}"
                ),
            )
        return GuardResult(allowed=True, guard_name=self.name)


class _BalanceLedgerProtocol(Protocol):
    def available_balance(self, symbol: str) -> Decimal: ...


class BuyingPowerGuard:
    """Block POST when offer_amount > available funding-wallet balance − buffer.

    Physical-funds twin of AllocationCapGuard (which enforces the policy cap).
    Defense-in-depth: the DeploymentReconciler's sizing clamp is the precise
    cumulative control; this is a per-offer backstop so an over-balance offer
    never leaves the process (avoids relying on the venue's 10001 rejection).
    SKIP/CANCEL bypass; exactly-at-(available−buffer) allows.

    Phase 2: the buffer is PER-SYMBOL. ``buffers`` maps symbol → buffer (e.g.
    {"fUST": 3, "fUSD": 3}). A symbol absent from ``buffers`` falls back to
    ``env_fallback_buffer`` (the legacy global BFX_BALANCE_BUFFER_USDT env value)
    when provided, else to ``default_buffer``. Available balance is read
    per-symbol so currency funding-wallet buckets stay isolated.
    """

    name = "buying_power"
    is_calibrated = False

    def __init__(
        self,
        *,
        ledger: _BalanceLedgerProtocol,
        buffers: dict[str, Decimal],
        default_buffer: Decimal,
        env_fallback_buffer: Decimal | None = None,
    ) -> None:
        self.ledger = ledger
        self._buffers = buffers
        self._default_buffer = default_buffer
        self._env_fallback = env_fallback_buffer

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if decision.decision_outcome != DecisionOutcome.POST:
            return GuardResult(allowed=True, guard_name=self.name)
        if decision.offer_amount_usdt is None:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason="POST decision missing offer_amount_usdt",
            )
        buffer = resolve_for_symbol(
            self._buffers, decision.symbol, self._env_fallback, self._default_buffer,
        )
        available = self.ledger.available_balance(decision.symbol)
        deployable = available - buffer
        offer = Decimal(str(decision.offer_amount_usdt))
        if offer > deployable:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=(
                    f"symbol={decision.symbol} offer={offer} > "
                    f"available={available}−buffer={buffer}={deployable}"
                ),
            )
        return GuardResult(allowed=True, guard_name=self.name)


class WriterLockGuard:
    """Fail-closed single-writer guard. Refuses every real-money submit unless
    this process still holds the Postgres advisory writer lock, verified LIVE
    against the dedicated connection (no stale-flag window)."""

    name = "writer_lock"
    is_calibrated = False

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
