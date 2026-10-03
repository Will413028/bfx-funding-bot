from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
)

from bfx_funding_bot.apps.bot_ports import ObservationVenue, select_bot_ports
from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS, load_config
from bfx_funding_bot.core.authority import Authority, read_authority
from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.core.errors import (
    EXIT_CODE_AUTH_FAILED,
    EXIT_CODE_WRITER_LOCKED,
    ConfigurationError,
    ExecutorAuthError,
    WriterLockUnacquired,
)
from bfx_funding_bot.core.health import HealthProbe
from bfx_funding_bot.core.schema_head import assert_schema_head
from bfx_funding_bot.core.telemetry import EventType, HealthStatus, HealthTarget, Level, Phase
from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.auth_ws import BitfinexAuthWSClient
from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient
from bfx_funding_bot.external.bitfinex.funding_rules import FundingRules
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.external.bitfinex.ws import (
    BitfinexWSClient,
    CandleMessage,
    ChannelSpec,
)
from bfx_funding_bot.modules.accounts.exchange_accounts import (
    account_id_uuid_or_none,
)
from bfx_funding_bot.modules.admin.trading_status import CapitalStatusReads, TradingStatusService
from bfx_funding_bot.modules.candles.repository import get_up_to, seal_closed_periods
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.deployments.identity import DeploymentIdentity
from bfx_funding_bot.modules.execution.audit import ExecutionDecisionRecorder
from bfx_funding_bot.modules.execution.bus import DomainEventBus
from bfx_funding_bot.modules.execution.capital_policy_control import CapitalPolicyRequestWorker
from bfx_funding_bot.modules.execution.command_boundary import CommandOutcomeNotice
from bfx_funding_bot.modules.execution.contracts import ExecutionPolicy
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.ladder import ladder_policy_from_env
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer
from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
from bfx_funding_bot.modules.execution.deployment.reprice import policy_from_env
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)
from bfx_funding_bot.modules.execution.deployment.tracker import CellDeploymentTracker
from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
    CreditClosed,
    OrderFilled,
    PositionReconciled,
    ReservationClaimed,
    ReservationReleased,
)
from bfx_funding_bot.modules.execution.fill_tracker import (
    RestPollingFillTracker,
)
from bfx_funding_bot.modules.execution.managed_cancel import ManagedOfferSweep
from bfx_funding_bot.modules.execution.middleware import (
    HeartbeatMiddleware,
    ReservationEmittingMiddleware,
)
from bfx_funding_bot.modules.execution.operator_requests import operator_authorized
from bfx_funding_bot.modules.execution.periodic_reconcile import PeriodicReconcile
from bfx_funding_bot.modules.execution.protocols import (
    CancelPort,
    ExecutorPort,
    FundingCancelAllPort,
    GuardRule,
)
from bfx_funding_bot.modules.execution.registry import build_executor
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.config import (
    load_safety_config,
)
from bfx_funding_bot.modules.execution.safety.hard_guards import (
    AllocationCapGuard,
    AuthHealthGuard,
    CapitalPolicyGuard,
    HeartbeatGuard,
    ManualKillGuard,
    UncertaintyGuard,
    WriterLockGuard,
)
from bfx_funding_bot.modules.execution.safety.kill_switch import QUIESCE_TIMEOUT_S, KillSwitch
from bfx_funding_bot.modules.execution.safety.nav_peak_store import NavPeakStore
from bfx_funding_bot.modules.execution.safety.nav_pnl_source import ReconcileNavTracker
from bfx_funding_bot.modules.execution.safety.nav_window_store import NavWindowStore
from bfx_funding_bot.modules.execution.safety.pre_trade import (
    build_command_throttle,
    build_pre_trade_guards,
    require_pre_trade_limits,
)
from bfx_funding_bot.modules.execution.safety.protection import (
    AutomaticProtection,
    NavDropMonitor,
    WriterLockWatch,
)
from bfx_funding_bot.modules.execution.safety.trading_state import (
    TradingStateRepository,
)
from bfx_funding_bot.modules.execution.trading_control import TradingControlWorker
from bfx_funding_bot.modules.execution.uncertainty_resolution import (
    ResolutionScope,
    UncertaintyResolutionWorker,
)
from bfx_funding_bot.modules.execution.venue_normalization_shadow import VenueNormalizationShadow
from bfx_funding_bot.modules.execution.ws_dispatcher import BitfinexLiveWSDispatcher
from bfx_funding_bot.modules.ledger import (
    ObservationSink,
    Scope,
    UnknownResolutionNotice,
    VenueHintNotification,
)
from bfx_funding_bot.modules.live_validation.credit_history import CreditHistorySync
from bfx_funding_bot.modules.live_validation.interest_ledger import (
    InterestLedgerSync,
    funding_currency,
)
from bfx_funding_bot.modules.live_validation.regime import record_config_regime
from bfx_funding_bot.modules.marketfeed.book_snapshot import BookSnapshotWriter
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.marketfeed.daemon import (
    Daemon,
    _DaemonAuditContextFactory,
    _emit_locf_degraded,
    _refuse_live_boot,
    assert_caps_invariant,
    assert_live_guard_invariant,
    load_account_bootstrap,
    log,
)
from bfx_funding_bot.modules.marketfeed.divergence_reporter import DivergenceReporter
from bfx_funding_bot.modules.marketfeed.funding_book import (
    FundingBookService,
    FundingBookStore,
)
from bfx_funding_bot.modules.marketfeed.health_monitor import HealthMonitor
from bfx_funding_bot.modules.marketfeed.readiness import TradingReadiness
from bfx_funding_bot.modules.marketfeed.scheduler import (
    Scheduler,
    last_candle_close_mts,
    now_ms_utc,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import StrategyRegistry
from bfx_funding_bot.modules.marketfeed.warmup import warmup_cell
from bfx_funding_bot.modules.observability import alerts
from bfx_funding_bot.modules.observability.metrics import (
    DaemonMetrics,
    MetricsSubmitMiddleware,
    TimedReconcileRecovery,
    attach_httpx_metrics,
    install_log_metrics_handler,
)
from bfx_funding_bot.modules.observability.resource import EventResource
from bfx_funding_bot.modules.observability.stdout_sink import StdoutEventSink
from bfx_funding_bot.modules.observability.tracing import (
    TracedReconcileRecovery,
    TracingSubmitMiddleware,
    instrument_ws_dispatcher,
    tracing_from_env,
)
from bfx_funding_bot.modules.strategy import CellConfig, configured_symbols
from bfx_funding_bot.modules.strategy.wiring import build_strategy, build_strategy_at_boundary

_BITFINEX_REST_BASE_URL = "https://api-pub.bitfinex.com"


async def build_daemon(
    *,
    cells_yaml_path: Path | None = None,
    skip_ws: bool = False,
) -> Daemon:
    config = load_config(cells_yaml_path=cells_yaml_path)
    db_engine = make_async_engine_from_url(config.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    live_executor = os.environ.get("BFX_EXECUTOR", "paper").strip().lower() == "bitfinex_live"
    if config.phase is Phase.LIVE and not live_executor:
        raise ConfigurationError("normal live requires bitfinex_live executor")
    try:
        allocation_cap = Decimal(
            "0" if config.phase is Phase.LIVE else os.environ.get("BFX_ALLOCATION_CAP_USDT", "500").strip()
        )
    except Exception as exc:
        raise ConfigurationError("BFX_ALLOCATION_CAP_USDT must be a decimal") from exc
    if not allocation_cap.is_finite() or allocation_cap < 0:
        raise ConfigurationError("BFX_ALLOCATION_CAP_USDT must be finite and >= 0")
    authority: Authority = "legacy"
    if live_executor:
        # Before the credential vault or anything else is read: a database at
        # another schema means this is the wrong build for it (for instance a
        # rollback onto a newer schema). The capital authority is read once,
        # right after: an authority this build does not support refuses too.
        try:
            async with session_factory() as schema_session:
                await assert_schema_head(schema_session)
                authority = await read_authority(schema_session)
        except Exception as exc:
            await _refuse_live_boot(exc, config=config, session_factory=session_factory)
            await db_engine.dispose()
            raise
    async with session_factory() as bootstrap_session:
        account_bootstrap = await load_account_bootstrap(
            bootstrap_session,
            deployment_environment=config.deployment_environment.value,
            allocation_cap_usdt=allocation_cap,
            phase=config.phase,
        )
    # Capital ports: the authority the database booted under picks every adapter a
    # consumer binds to (apps/bot_ports.py); nothing below names an authority.
    env_str = config.deployment_environment.value
    capital_scope = Scope(account_bootstrap.exchange_account_id, env_str)
    account_id = account_bootstrap.account_id
    bus = DomainEventBus()
    ports = await select_bot_ports(
        authority, session_factory=session_factory, scope=capital_scope, account_id=account_id,
        bus=bus, live=live_executor, clock=now_ms_utc,
        max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS,
    )
    capital = ports.capital
    uncertainty_reader = ports.uncertainty_reader
    managed_offers = ports.managed_offers
    legacy = ports.legacy
    paper_ledger = legacy.paper_ledger if legacy is not None else None
    if capital is not None:
        try:
            # Before anything can trade: every configured currency has an
            # applied capital policy.
            async with session_factory.begin() as policy_session:
                for symbol in configured_symbols(config.cells):
                    await capital.policy_store.read_applied(policy_session, symbol=symbol)
        except Exception as exc:
            await _refuse_live_boot(exc, config=config, session_factory=session_factory)
            await db_engine.dispose()
            raise

    probe = HealthProbe()
    # ── Four Golden Signals metrics (observe-only; /metrics on healthz srv) ──
    # Wired FIRST so every sink / client / wrapper below can carry the hook.
    # All recording is fail-open — a metrics fault never touches trading paths.
    metrics = DaemonMetrics()
    metrics.register_probe(probe)  # heartbeat age/threshold + health_status
    install_log_metrics_handler(metrics)  # WARNING+ error-rate, idempotent
    trading_readiness = TradingReadiness(on_change=metrics.set_trading_ready)
    # deployment_environment comes from config (BFX_DEPLOYMENT_ENV via load_config).
    event_resource = EventResource(
        deployment_environment=config.deployment_environment,
    )
    metrics.set_daemon_info(
        service_version=event_resource.service_version,
        deployment_environment=config.deployment_environment.value,
        phase=config.phase.value,
    )
    # OTel traces (wiki pending #4) — DEFAULT OFF via BFX_OTEL_ENABLED. When
    # disabled this is a no-op object (zero SDK init) and NOTHING below gets
    # wrapped, so the money-path call chain is byte-identical to the
    # metrics-era stack. All span recording is fail-open.
    tracing = tracing_from_env(os.environ, event_resource=event_resource)
    if tracing.enabled:
        log.info("otel_tracing_enabled endpoint=%s", tracing.endpoint)
    stdout_sink = StdoutEventSink(resource=event_resource, metrics=metrics)
    bitfinex_http = httpx.AsyncClient()
    # Venue REST traffic/latency/error metrics — additive event hooks on the
    # ONE shared client (public REST + auth REST + live executor + tracker).
    attach_httpx_metrics(bitfinex_http, metrics)
    bitfinex = BitfinexREST(
        http=bitfinex_http,
        base_url=_BITFINEX_REST_BASE_URL,
        limiter=FundingRateLimiter(),
    )
    funding_book_service: FundingBookService | None = None
    if config.execution_policy is not ExecutionPolicy.PAPER:
        if (
            config.book_max_age_seconds is None
            or config.book_reconcile_interval_seconds is None
            or config.book_max_down_pct is None
        ):
            raise ValueError(
                "book-capable execution policy requires FundingBookService configuration",
            )
        funding_book_service = FundingBookService(
            store=FundingBookStore(max_age_seconds=config.book_max_age_seconds),
            rest=bitfinex,
            ws=FundingBookWSClient(symbols=sorted(configured_symbols(config.cells))),
            symbols=sorted(configured_symbols(config.cells)),
            reconcile_interval_seconds=config.book_reconcile_interval_seconds,
        )
    registry = StrategyRegistry(build_strategy)
    monitor = HealthMonitor(phase=config.phase, event_sink=stdout_sink, probe=probe)
    candle_q: asyncio.Queue[CandleMessage | None] = asyncio.Queue()

    now_mts = now_ms_utc()
    async with session_factory() as session:
        for cell in config.cells:
            try:
                await warmup_cell(
                    cell=cell,
                    registry=registry,
                    boundary_builder=build_strategy_at_boundary,
                    bitfinex=bitfinex,
                    session=session,
                    now_mts=now_mts,
                )
            except Exception:
                log.exception(
                    "warmup_cell_failed cell=%s — skipping", cell.pair_id,
                )
        probe.update(HealthTarget.BITFINEX_REST, HealthStatus.HEALTHY, latency_ms=0)
        probe.update(HealthTarget.DB, HealthStatus.HEALTHY, latency_ms=0)

    class _CandlesRepoBridge:
        async def get_up_to(
            self,
            *,
            symbol: str,
            timeframe: str,
            period_agg: str,
            mts_inclusive: int,
            lookback: int,
        ) -> list[FundingCandle]:
            async with session_factory() as s:
                return await get_up_to(
                    s,
                    symbol=symbol,
                    timeframe=timeframe,
                    period_agg=period_agg,
                    mts_inclusive=mts_inclusive,
                    lookback=lookback,
                )

    # ── Halt 1 account bootstrap ────────────────────────────────────────
    # The immutable UUID, vault credential and optional config draft were
    # loaded before any venue client or worker was constructed. Every service
    # below receives this one canonical identity; no env realm is read here.
    credentials = account_bootstrap.credentials
    account_ctx = account_bootstrap.to_context()
    # ONE µs nonce gate shared by every auth client on this single API key.
    # Bitfinex nonces are per-key across REST *and* WS, so mixed scales /
    # independent time-based providers get "nonce: small" rejections — that is
    # what left the auth WS flapping — and concurrent signed requests must also
    # ARRIVE in nonce order, so the gate serializes them. See
    # external/bitfinex/nonce.py.
    bfx_auth_gate = AuthRequestGate()

    diagnostics = DiagnosticsSink(
        session_factory=session_factory, account_id=account_id,
        deployment_environment=env_str,
        metrics=metrics,  # bfx_diagnostic_events_total (safety trips / decisions)
    )
    # Safety config — immutable for daemon lifetime. Config change = redeploy.
    safety_cfg_path = Path(
        os.environ.get("BFX_SAFETY_CONFIG", "configs/safety.yaml"),
    )
    safety_cfg = load_safety_config(safety_cfg_path)
    assert_live_guard_invariant(config.phase, safety_cfg)
    hg = safety_cfg.hard_guards
    if capital is None:
        assert_caps_invariant(config.cells, hg.allocation_cap)

    # Automatic protections (lending envelope D3 level 3): trips stop new offers
    # at once and queue HALTED/auto + a managed-offer cancel, which a supervised
    # task performs outside every lock once the kill switch is bound below.
    protection = AutomaticProtection(clock=now_ms_utc)

    # NAV (available + reserved + realized) sampled from each reconcile
    # snapshot, for the NAV-drop alert (level 4). Subscribed to
    # PositionReconciled below (alongside the ledger).
    window_account = account_id_uuid_or_none(account_id)
    pnl_source = ReconcileNavTracker(
        account_id=account_id,
        peak_store=NavPeakStore(
            session_factory, account_id=account_id, deployment_environment=env_str,
        ),
        # T9: the 24h loss window survives restarts (canonical accounts only).
        window_store=(NavWindowStore(session_factory, account_id=window_account,
                                     deployment_environment=env_str)
                      if window_account is not None else None),
    )
    # Seed the all-time peak from nav_peak so drawdown_pct survives restarts
    # (fail-permissive: load errors leave the in-memory-only behavior).
    await pnl_source.load_persisted_peaks()
    await pnl_source.load_persisted_window()

    # Durable trading state. Built unconditionally (like NavPeakStore) so every
    # phase can be stopped durably. Note the OPPOSITE failure posture to the
    # peak store above: an unreadable trading state blocks submits rather than
    # degrading gracefully — see ManualKillGuard.
    trading_state = TradingStateRepository(
        session_factory, account_id=account_id, deployment_environment=env_str,
    )

    # cells[0] used for safety_chain emit envelope (phase/strategy/cell) —
    # 4.2 is single-cell paper / shadow; multi-cell uniform-policy refinement
    # tracked in Phase 4.4. AllocationCap is account-scoped (not per-cell),
    # so the envelope labels are informational only.
    first_cell = config.cells[0]

    # M2: SafetyConfig.<guard>.enabled is honoured at build time — disabled
    # guards are not constructed (cleaner than relying on internal no-op).
    # Live (real money) cannot boot with a required guard off
    # (assert_live_guard_invariant); paper/shadow may disable them.
    # Phase 2: every configured currency must have an explicit cap in simulation
    # — config-fatal otherwise. Then log the effective cap per symbol so
    # the boot log is the authoritative record of how much real money each
    # currency may deploy.
    log.info(
        "effective_cap_per_symbol %s",
        # assert_caps_invariant (above) already proved every configured symbol has
        # an explicit caps entry in ALL phases, so direct indexing can't KeyError.
        "applied CapitalPolicy" if capital is not None else
        {s: hg.allocation_cap.caps[s] for s in configured_symbols(config.cells)},
    )
    # Single available-buffer bound shared by the per-offer BuyingPowerGuard and
    # the cumulative DeploymentReconciler clamp — read once so both consume the
    # same value (no double subtraction).
    balance_buffer_usdt = Decimal("0") if capital is not None else Decimal(os.environ.get("BFX_BALANCE_BUFFER_USDT", "3"))

    # Executor is built before the guard chain so guard composition can branch on
    # spec.is_simulated (BuyingPowerGuard is live-only — see allocation_cap block).
    # bus is the live executor's construction dependency, so it is created here.
    # Env-driven via registry (CC4 invariant — paper + fill_tracker rejected;
    # bitfinex_live rejected in 4.2; 4.4 enables live path).
    all_symbols = frozenset(configured_symbols(config.cells))
    spec = build_executor(
        event_sink=stdout_sink,
        phase=config.phase,
        strategy=first_cell.strategy,
        configured_symbols=all_symbols,
        cell=first_cell.cell_id,
        http=bitfinex_http,
        bus=bus,
        auth_gate=bfx_auth_gate,
    )
    if not spec.is_simulated and capital is None:
        raise ConfigurationError("live executor requires applied capital runtime")

    # Single-writer advisory lock (A1). Construct LIVE-ONLY (not spec.is_simulated)
    # so paper/shadow leave it None and the guard/liveness checks are inert.
    # ACQUIRE only on Postgres: sqlite wiring tests construct the object but must
    # never touch a real lock; the boot acquire raises WriterLockUnacquired on
    # contention → propagates to main() → sys.exit(EXIT_CODE_WRITER_LOCKED). It is
    # built here (before the guards block + the Daemon return) so the same variable
    # is in scope at both the guard-append and the return.
    writer_lock: WriterLock | None = None
    if not spec.is_simulated:
        writer_lock = WriterLock(
            database_url=config.database_url,
            key=derive_lock_key(account_id, env_str),
        )
        if config.database_url.startswith(("postgres", "postgresql")):
            await writer_lock.acquire()  # raises WriterLockUnacquired on contention

    guards: list[GuardRule] = []
    if hg.manual_kill.enabled:
        guards.append(
            ManualKillGuard(
                trading_state=trading_state,
                pending_stop=protection.pending_reason,
            )
        )
    # UNKNOWN/orphan exposure is always account+environment+symbol scoped and
    # must be read before the reconciler computes an economic gap.  It is not
    # configurable off in canary/live because an unreadable projection fails
    # closed inside the guard.
    guards.append(UncertaintyGuard(
        reader=uncertainty_reader,
        deployment_environment=env_str,
    ))
    if hg.auth_health.enabled:
        guards.append(AuthHealthGuard(probe=probe))
    if hg.heartbeat.enabled:
        guards.append(HeartbeatGuard(
            probe=probe,
            threshold_seconds=hg.heartbeat.sub_task_stale_threshold_seconds,
            # Readiness gate: block POST only when our MARKET VIEW is stale.
            # Watch market-data own-loop liveness ("ws"), not the reactive
            # executor/safety_chain — those are bumped only by trading itself,
            # so watching them self-suppresses trades in quiet markets and was
            # part of the 2026-05-26 canary restart loop. ws stays fresh in
            # quiet markets via _ws_heartbeat_poll_loop (Bitfinex hb ~15s).
            watched_sub_tasks=["ws"],
        ))
    if capital is not None:
        guards.append(CapitalPolicyGuard(authority=capital.capital_authority, scope=capital_scope,
                                         clock=now_ms_utc))
        # Always-on offer envelope (fail-closed); the command throttle config is
        # required for a live writer too.
        require_pre_trade_limits(safety_cfg.pre_trade_limits)
        guards.extend(build_pre_trade_guards(
            authority=capital.capital_authority, offers=managed_offers, scope=capital_scope,
            session_factory=session_factory, book=funding_book_service, clock=now_ms_utc))
    elif hg.allocation_cap.enabled:
        assert paper_ledger is not None  # no capital authority: the simulated, legacy composition
        guards.append(AllocationCapGuard(
            ledger=paper_ledger,
            caps=hg.allocation_cap.caps,
            default_cap=hg.allocation_cap.default_cap,
            # Legacy simulation-only configuration. Live always uses the policy above.
            env_fallback_cap=allocation_cap,
        ))
    # Fail-closed single-writer guard — live-only (writer_lock is None on
    # paper/shadow). Authoritative per-submit liveness via verify_held().
    if writer_lock is not None:
        guards.append(WriterLockGuard(lock=writer_lock))

    safety_chain = SafetyGuardChain(
        guards=guards,
        probe=probe,
        diagnostics=diagnostics,
        phase=config.phase,
        strategy=first_cell.strategy,
        cell=first_cell.cell_id,
        account_id=account_id,
    )

    # ---- Phase 4.3/4.4a executor middleware chain wiring ----
    # bus + spec (executor) are built above the guard chain (guard composition
    # branches on spec.is_simulated).
    quote_store = StandingQuoteStore(
        ttl_ms=int(os.environ.get("BFX_QUOTE_TTL_MS", "3900000")),
    )

    executor: ExecutorPort = spec.executor

    # 3a-recovery: live-only venue reconciliation. Paper/shadow have no real
    # venue offers (BFX_FILL_TRACKER/WS gated off) -> boot_recovery stays None
    # and Daemon.run() skips it.
    boot_recovery: ObservationSink | None = None
    periodic_reconcile: PeriodicReconcile | None = None
    book_snapshot_writer: BookSnapshotWriter | None = None
    interest_ledger_sync: InterestLedgerSync | None = None
    credit_history_sync: CreditHistorySync | None = None
    if not spec.is_simulated:
        auth_rest = BitfinexAuthREST(
            http=bitfinex_http, auth_gate=bfx_auth_gate,
            response_observer=VenueNormalizationShadow(),
        )
        # Realized income truth (ledger category 28), read-only: see interest_ledger.
        interest_ledger_sync = InterestLedgerSync(
            rest=auth_rest, ctx=account_ctx, session_factory=session_factory,
            exchange_account_id=account_bootstrap.exchange_account_id,
            deployment_environment=env_str,
            currencies=sorted({funding_currency(s) for s in configured_symbols(config.cells)}),
        )
        # Per-credit truth (credit history + funding trades), read-only: see credit_history.
        credit_history_sync = CreditHistorySync(
            rest=auth_rest, ctx=account_ctx, session_factory=session_factory,
            exchange_account_id=account_bootstrap.exchange_account_id,
            deployment_environment=env_str,
            symbols=sorted(configured_symbols(config.cells)),
        )
        assert capital is not None  # validated immediately after executor construction
        reconcile_interval_s = float(os.environ.get("BFX_RECONCILE_INTERVAL_S", "90"))
        if reconcile_interval_s <= 0:
            raise ValueError(
                f"BFX_RECONCILE_INTERVAL_S must be > 0, got {reconcile_interval_s}"
            )
        resync_min_interval_s = float(os.environ.get("BFX_RESYNC_MIN_INTERVAL_S", "10"))
        if resync_min_interval_s < 0:
            raise ValueError(
                f"BFX_RESYNC_MIN_INTERVAL_S must be >= 0, got {resync_min_interval_s}"
            )
        sinks = capital.observation(ObservationVenue(
            auth_rest=auth_rest, account_ctx=account_ctx, protection=protection,
            symbols=configured_symbols(config.cells),
            cells=[(cell.symbol, cell.cell_id) for cell in config.cells],
        ))
        boot_recovery = sinks.boot
        runtime_recovery = sinks.runtime

    # The event-log authority keeps an in-memory exposure projection and a CID offer
    # registry fed from the bus; a ledger-authority process has neither, and its bus only
    # carries notifications after the writing transaction committed.
    if legacy is not None:
        assert paper_ledger is not None
        bus.subscribe(ReservationClaimed,  paper_ledger.on_reservation_claimed)
        bus.subscribe(OrderFilled,         paper_ledger.on_order_filled)
        bus.subscribe(ReservationReleased, paper_ledger.on_reservation_released)
        # Phase 4.4a: OfferRegistry projection — stays in sync with event log.
        bus.subscribe(ReservationClaimed,  legacy.offer_registry.handle)
        bus.subscribe(OrderFilled,         legacy.offer_registry.handle)
        bus.subscribe(ReservationReleased, legacy.offer_registry.handle)
    # 3b: cancel lifecycle → diagnostics (CANCEL_AUDIT). Forensic, best-effort.
    bus.subscribe(CancelRequested,     diagnostics.handle_cancel_requested)
    bus.subscribe(CancelAcknowledged,  diagnostics.handle_cancel_acknowledged)
    # Credit-aware reconcile: PositionReconciled is the ledger's sole exposure
    # authority at reconcile time (recovery FSM events are routed to the registry,
    # not the bus — see BootRecovery._route_fsm). Wiring both together is required:
    # subscribing here without the registry routing would double-count orphans.
    if paper_ledger is not None:
        bus.subscribe(PositionReconciled, paper_ledger.on_position_reconciled)
    # Same snapshot feeds the NAV tracker (peak + 24h window) behind the NAV-drop
    # alert, wrapped so it reads metrics the tracker already updated.
    nav_drop = NavDropMonitor(
        source=pnl_source,
        realized_loss_threshold_pct=safety_cfg.nav_alerts.realized_loss_24h_pct,
        drawdown_threshold_pct=safety_cfg.nav_alerts.drawdown_pct,
    )
    bus.subscribe(PositionReconciled, nav_drop.on_position_reconciled)
    # Traffic signal: bfx_domain_events_total{event_type} — one fail-open
    # counting handler across all execution domain events (observe-only; a
    # handler failure is already isolated by the bus's per-handler gather).
    domain_event_counter = metrics.domain_event_handler()
    for _domain_event_type in (
        ReservationClaimed, OrderFilled, ReservationReleased,
        CancelRequested, CancelAcknowledged, PositionReconciled, CreditClosed,
        # What the ledger authority publishes after its transactions committed.
        CommandOutcomeNotice, UnknownResolutionNotice, VenueHintNotification,
    ):
        bus.subscribe(_domain_event_type, domain_event_counter)

    # NO retry wrapper around submit: a funding-offer submit is a financial write
    # that must be attempted exactly once. Bitfinex funding offers have no client
    # cid dedup (only trading orders do), so retrying a submit would risk a real
    # duplicate live offer. Transient failures are recovered by the periodic
    # reconcile, not by re-submitting. (cancel retries internally — it's idempotent.)
    # MetricsSubmitMiddleware is OUTERMOST and observe-only: times the full
    # submit chain (intent persist + venue POST + ack) into
    # bfx_executor_submit_duration_seconds and counts outcomes. It re-raises /
    # returns unchanged, so the HeartbeatMiddleware I1 invariant and the
    # no-retry submit contract below are untouched.
    reservation_middleware = ReservationEmittingMiddleware(
        executor,
        bus=bus,
        persister=legacy.persister if legacy is not None else None,
        is_simulated=spec.is_simulated,
        safety_evaluator=safety_chain,
        boundary=capital.command_boundary if capital is not None else None,
        uncertainty_reader=uncertainty_reader,
        managed_offers=managed_offers if capital is not None else None,
    )
    reservation_executor: ExecutorPort = reservation_middleware

    wrapped_executor: ExecutorPort = MetricsSubmitMiddleware(
        HeartbeatMiddleware(reservation_executor, probe=probe),
        metrics=metrics,
    )
    # OTel span "executor.submit" — enabled-only, stacked OUTSIDE metrics so
    # one span covers the full chain. Transparent: result/exception unchanged.
    if tracing.enabled:
        wrapped_executor = TracingSubmitMiddleware(wrapped_executor, tracing=tracing)


    # Last-submit-attempt slot read by GET /admin/trading-status. Built
    # unconditionally so the endpoint always has a `started_at` to report;
    # only the live reconciler ever writes to it, so on paper/shadow it stays
    # empty — which correctly reads as "this process has submitted nothing".
    attempt_recorder = SubmitAttemptRecorder()

    deployment_reconciler = None
    trading_control: TradingControlWorker | None = None
    capital_policy_control: CapitalPolicyRequestWorker | None = None
    # What the deploy tool says this build is; it names every decision's build
    # and the status report's, and never gates trading (lending envelope D5).
    deployment_identity = DeploymentIdentity.from_env(os.environ)
    uncertainty_worker = None
    if not spec.is_simulated:
        if funding_book_service is None:
            raise ValueError(
                "live execution requires a FundingBookService for the configured policy",
            )
        assert capital is not None  # validated immediately after executor construction
        reprice_policy = policy_from_env(os.environ)
        ladder_policy = ladder_policy_from_env(os.environ)
        execution_gate = ExecutionGate(
            policy=config.execution_policy,
            audit=ExecutionDecisionRecorder(session_factory),
            readiness=trading_readiness,
            events=stdout_sink,
            metrics=metrics,
        )
        funding_rules = FundingRules(http=bitfinex_http, clock=now_ms_utc)
        trading_control = TradingControlWorker(
            session_factory=session_factory, account_id=UUID(account_id),
            environment=env_str, authority=operator_authorized, clock=now_ms_utc,
            ownership=writer_lock.verify_held if writer_lock is not None else None,
        )
        capital_policy_control = CapitalPolicyRequestWorker(
            session_factory=session_factory, account_id=UUID(account_id),
            environment=env_str, authority=operator_authorized,
            policy_store=capital.policy_store, scope_lock=capital.scope_lock,
            clock=now_ms_utc,
            ownership=writer_lock.verify_held if writer_lock is not None else None,
        )
        deployment_reconciler = DeploymentReconciler(
            capital=capital.capital_authority,
            offers=managed_offers,
            uncertainty=uncertainty_reader,
            scope=capital_scope,
            session_factory=session_factory,
            store=quote_store,
            tracker=CellDeploymentTracker(),
            ledger=paper_ledger,
            safety_chain=safety_chain,
            executor=wrapped_executor,
            account_ctx=account_ctx,
            cells=config.cells,
            funding_rules=funding_rules,
            clock=now_ms_utc,
            event_sink=stdout_sink,
            phase=config.phase,
            canceller=reservation_middleware if isinstance(executor, CancelPort) else None,
            reprice=reprice_policy,
            ladder=ladder_policy,
            attempt_recorder=attempt_recorder,
            book_provider=funding_book_service,
            execution_gate=execution_gate,
            execution_policy=config.execution_policy,
            optimizer_fee_rate=config.optimizer_fee_rate,
            period_pricer=PeriodPricer(
                max_down_pct=Decimal(str(config.book_max_down_pct)),
                tick=Decimal("0.00000001"),
            ),
            # Every decision names the build that made it: the revision and
            # image digest the deploy tool injected.
            audit_context_factory=_DaemonAuditContextFactory(
                account_id=account_id,
                deployment_environment=env_str,
                service_version=deployment_identity.source_revision or "unidentified",
                config_hash=deployment_identity.backend_digest or "unidentified",
            ),
            protection=protection,
            managed_sweep=(ManagedOfferSweep(
                session_factory=session_factory, account_id=UUID(account_id),
                environment=env_str, canceller=reservation_middleware, ctx=account_ctx,
                offers=managed_offers)
                if isinstance(executor, CancelPort) else None),
        )
        assert writer_lock is not None
        # The web API queues operator adjudications; only this writer appends them.
        uncertainty_worker = UncertaintyResolutionWorker(
            session_factory=session_factory,
            scope=ResolutionScope(account_bootstrap.exchange_account_id, env_str),
            authority=operator_authorized,
            clock=now_ms_utc,
            ownership=writer_lock.verify_held,
            resolution=capital.operator_resolution,
        )
        # Execution-policy regime telemetry: one row per boot (flags are
        # boot-immutable, so boots are the regime boundaries). Best-effort —
        # record_config_regime never raises.
        await record_config_regime(
            session_factory,
            account_id=account_id,
            deployment_environment=env_str,
            clamp_enabled=False,
            reprice_enabled=reprice_policy.enabled,
            git_sha=deployment_identity.source_revision,
            now_ms=now_ms_utc(),
        )
        # Transparent timing shim (bfx_reconcile_tick_duration_seconds /
        # bfx_reconcile_ticks_total) — PeriodicReconcile's failure handling
        # sees exactly what the raw recovery would produce. When tracing is
        # enabled, the "reconcile.tick" span wrapper stacks OUTSIDE the timer
        # (same window as the histogram); both are observe-only pass-throughs.
        recovery_runner: ObservationSink = (
            TimedReconcileRecovery(runtime_recovery, metrics=metrics)
        )
        if tracing.enabled:
            recovery_runner = TracedReconcileRecovery(recovery_runner, tracing=tracing)
        periodic_reconcile = PeriodicReconcile(
            recovery=recovery_runner,
            scope=capital_scope,
            probe=probe,
            interval_s=reconcile_interval_s,
            min_resync_interval_s=resync_min_interval_s,
            deployment=deployment_reconciler,
            deployment_input=capital.deployment_input,
        )

    # The hint sink needs the reconcile's resync request, so it is built after it. WS and
    # REST polling share the one sink: a ledger sink debounces per instance.
    venue_hint_sink = (
        ports.venue_hint_sink(
            periodic_reconcile.request_resync if periodic_reconcile is not None else None)
        if spec.fill_tracker_enabled or spec.ws_client_enabled else None
    )
    fill_tracker: RestPollingFillTracker | None = None
    if venue_hint_sink is not None and spec.fill_tracker_enabled:
        fill_tracker = RestPollingFillTracker(
            http=bitfinex_http,
            event_sink=stdout_sink,
            probe=probe,
            phase=config.phase,
            strategy=first_cell.strategy,
            cell=first_cell.cell_id,
            account_id=account_id,
            venue_hint_sink=venue_hint_sink,
        )

    # ---- Phase 4.4 prework: SmokeRunner ----
    from bfx_funding_bot.modules.admin.pg_event_log_query import PostgresEventLogQueryAdapter
    from bfx_funding_bot.modules.admin.smoke_runner import SmokeRunner

    # Simulated-only: the L2/L3 smoke submits a probe order through wrapped_executor
    # with placeholder creds, expecting a simulated "filled". Against a live executor
    # that is a real venue POST with bogus creds (→ 10100 "apikey: digest invalid"),
    # or with real creds a real boot-time order. So wire smoke only when simulated;
    # live (canary) leaves it None → boot smoke skips + HTTP /admin/smoke-test unmounted.
    smoke_runner: SmokeRunner | None = None
    if spec.is_simulated:
        smoke_pg_query = PostgresEventLogQueryAdapter(
            session_factory=session_factory,
            deployment_environment=env_str,
        )
        smoke_runner = SmokeRunner(
            executor=wrapped_executor,
            bus=bus,
            pg_query=smoke_pg_query,
            phase=config.phase,
            strategy=first_cell.strategy,
            cell=first_cell.cell_id,
        )

    # ---- Phase 4.3 replay invariant report ----
    if paper_ledger is not None and paper_ledger.replay_floor_hit_count > 0:
        probe.update(
            HealthTarget.LEDGER, HealthStatus.DEGRADED,
            error_message=f"{paper_ledger.replay_floor_hit_count} floor hits during replay",
        )
        await stdout_sink.emit({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": Level.WARN.value,
            "phase": config.phase.value,
            "strategy": None, "cell": None,
            "event_type": EventType.HEALTH_CHECK.value,
            "correlation_id": str(uuid4()),
            "payload": {
                "check_target": HealthTarget.LEDGER.value,
                "status": HealthStatus.DEGRADED.value,
                "error_message": f"replay_floor_hit_count={paper_ledger.replay_floor_hit_count}",
            },
        })

    signal_engine_obj = SignalEngine(
        phase=config.phase,
        event_sink=stdout_sink,
        diagnostics=diagnostics,
        candles_repo=_CandlesRepoBridge(),
        reporter=DivergenceReporter(build_strategy_at_boundary),
        quote_store=quote_store,
        clock=lambda: int(time.time() * 1000),
    )

    async def on_scheduler_tick(
        cell: CellConfig, mts: int, *, quote_created_at_ms: int | None = None,
    ) -> None:
        # Scheduler fires AT the period boundary T (e.g. 11:00 UTC), but
        # Bitfinex's candle at mts=T is the OPEN of period [T, T+timeframe) —
        # only the FIRST tick of the new period creates it, which may not
        # arrive within the 5s buffer. The candle we actually want is the
        # one that JUST CLOSED — by Bitfinex start-of-period convention,
        # that has mts = T - timeframe.
        # Discovered Phase 4.2.0 d90363fa shadow run: 56 degraded events,
        # 0 signal events because query used mts=T (always empty).
        from bfx_funding_bot.modules.marketfeed.scheduler import _TIMEFRAME_MS
        candle_mts = mts - _TIMEFRAME_MS[cell.timeframe]

        # Phase 4.3 LOCF: fetch a lookback window and LOCF-fill gaps.
        # staleness_budget_hours is guaranteed non-None after load_config().
        budget_hours: int = cell.staleness_budget_hours  # type: ignore[assignment]
        # lookback here is for staleness-tier determination only:
        # reindex_and_ffill needs candles within `budget_hours` to decide hard vs soft
        # tier. Strategy state itself lives in the pre-warmed StrategyRegistry which
        # was fed during daemon startup — NOT re-fed from this small window.
        lookback = budget_hours + 1  # +1 to ensure boundary candle is included
        async with session_factory() as s:
            # Seal on elapsed time before reading. CandleWriter only seals when a
            # LATER mts lands, so a slow venue push leaves mts=candle_mts unsealed
            # at exactly the moment we need it — a final-only read then skips it,
            # LOCF fills the slot from the previous candle, and the live observe
            # sequence gains a hole that replay does not have.
            await seal_closed_periods(
                s,
                symbol=cell.symbol,
                timeframe=cell.timeframe,
                period_agg=cell.period_agg,
            )
            await s.commit()
            raw_candles = await get_up_to(
                s,
                symbol=cell.symbol,
                timeframe=cell.timeframe,
                period_agg=cell.period_agg,
                mts_inclusive=candle_mts,
                lookback=lookback,
            )

        filled = reindex_and_ffill(
            raw_candles, ref_mts=candle_mts, max_gap_hours=budget_hours,
        )

        log.info(
            "scheduler_tick cell=%s boundary_mts=%d candle_mts=%d raw=%d filled=%d",
            cell.pair_id, mts, candle_mts, len(raw_candles), len(filled),
        )

        # pair_id = strategy + cell_id; used as state key so two strategies on
        # the same cell are independent SIGNAL_PIPELINE state machines
        # (e.g. RP can be DEGRADED while MR is HEALTHY).
        if not filled:
            # No candles at all in lookback window — treat as hard-tier DEGRADED.
            budget_seconds = budget_hours * 3600
            if probe.get_cell_pipeline_status(cell.pair_id) != HealthStatus.DEGRADED:
                probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.DEGRADED)
                await _emit_locf_degraded(
                    stdout_sink, config, cell,
                    stale_seconds=None,  # no candles → no meaningful age
                    budget_seconds=budget_seconds,
                )
            return

        latest = filled[-1]

        if latest.candle is None:
            # Hard tier: stale_seconds > budget — skip signal, emit DEGRADED once.
            budget_seconds = budget_hours * 3600
            if probe.get_cell_pipeline_status(cell.pair_id) != HealthStatus.DEGRADED:
                probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.DEGRADED)
                await _emit_locf_degraded(
                    stdout_sink, config, cell,
                    stale_seconds=latest.stale_seconds,
                    budget_seconds=budget_seconds,
                )
            return

        # Soft tier or fresh — emit signal with staleness metadata.
        if probe.get_cell_pipeline_status(cell.pair_id) == HealthStatus.DEGRADED:
            # HEALTHY restore transition
            probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.HEALTHY)
            await stdout_sink.emit({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": Level.INFO.value,
                "phase": config.phase.value,
                "strategy": None,
                "cell": cell.cell_id,
                "event_type": EventType.HEALTH_CHECK.value,
                "correlation_id": str(uuid4()),
                "payload": {
                    "check_target": HealthTarget.SIGNAL_PIPELINE.value,
                    "status": HealthStatus.HEALTHY.value,
                },
            })
        else:
            probe.set_cell_pipeline_status(cell.pair_id, HealthStatus.HEALTHY)

        # Unwrap LOCF-filled candles for strategy compute — strategies receive
        # FundingCandle objects with forward-filled rates (LOCF semantic).
        await signal_engine_obj.process_candle(
            cell=cell,
            candle=latest.candle,
            registry=registry,
            is_stale=latest.is_stale,
            stale_seconds=latest.stale_seconds,
            quote_created_at_ms=quote_created_at_ms,
        )

    scheduler = Scheduler(
        callback=on_scheduler_tick,
        probe=probe,
        buffer_s=config.scheduler_buffer_s,
    )

    # Replay the boundary a cold start would otherwise skip.
    #
    # StandingQuoteStore is in-memory, so a restart empties it, and the scheduler
    # arms the NEXT boundary -- a process that comes up at 07:01 deploys nothing
    # until 08:00. That is up to a full timeframe of idle capital bought by a
    # restart, which every deploy would otherwise pay.
    #
    # Nothing about the boundary just closed is unavailable here. Its candle is
    # sealed and in Postgres, the strategy registry is already warmed above, and
    # the tick is a pure function of the two. So run the real tick for exactly
    # the boundary `register_from_now` skips -- the same code path, because a
    # rebuild that could diverge from the live one would be worse than none.
    #
    # The signal layer writes no ledger: its SIGNAL/DECISION events go to the
    # stdout sink. Replaying a boundary therefore adds telemetry, not history.
    for cell in config.cells:
        boundary = last_candle_close_mts(timeframe=cell.timeframe, now_ms=now_ms_utc())
        try:
            await on_scheduler_tick(cell, boundary, quote_created_at_ms=boundary)
        except Exception:
            # Fail open: a quote that cannot be rebuilt is the cold start we
            # already had, and is never a reason to refuse to boot.
            log.exception(
                "standing_quote_rehydrate_failed cell=%s mts=%d", cell.pair_id, boundary,
            )
        else:
            log.info(
                "standing_quote_rehydrated cell=%s boundary_mts=%d", cell.pair_id, boundary,
            )
    writer = CandleWriter(queue=candle_q, session_factory=session_factory, probe=probe)

    ws_client: BitfinexWSClient | None = None
    if not skip_ws:
        ws_client = BitfinexWSClient(
            channels=[
                ChannelSpec(
                    symbol=c.symbol,
                    timeframe=c.timeframe,
                    period_agg=c.period_agg,
                )
                for c in config.cells
            ],
            on_disconnect=lambda reason: probe.update(
                HealthTarget.BITFINEX_WS,
                HealthStatus.DEGRADED,
                last_msg_age_ms=999_999,
                reconnect_count_last_hour=0,
                error_message=f"ws_disconnect: {reason}",
            ),
        )

    # ---- Kill switch: HALTED, then the venue funding cancel-all ----
    # The only venue write that bypasses the command gate; it needs the writer
    # lock and nothing else. Paper/shadow have no venue, so they record the
    # stop and skip the cancel-all.
    command_gate = reservation_middleware.command_gate
    kill_switch = KillSwitch(
        trading_state=trading_state,
        session_factory=session_factory,
        ctx=account_ctx,
        configured_symbols=all_symbols,
        venue=(executor if not spec.is_simulated and isinstance(executor, FundingCancelAllPort)
               else None),
        writer_lock=writer_lock,
        uncertainty=uncertainty_reader,
        offers=managed_offers,
        quiesce=(
            (lambda: command_gate.quiesced(account_id, timeout_s=QUIESCE_TIMEOUT_S))
            if command_gate is not None else None
        ),
        clock=now_ms_utc,
    )
    # An automatic stop writes HALTED alone; the planner then pulls managed offers.
    protection.bind(trading_state)
    if trading_control is not None:
        trading_control.kill_switch = kill_switch  # the operator's kill request
    if command_gate is not None:
        command_gate.protection = protection
        if safety_cfg.pre_trade_limits is not None:
            command_gate.throttle = build_command_throttle(
                safety_cfg.pre_trade_limits, protection=protection)

    # ---- GET /admin/trading-status + POST /admin/dry-evaluate ----
    # Real-money status uses the same applied policy reader as the planner and
    # command gate. Legacy scalar/map arguments are simulation diagnostics only.
    trading_status = TradingStatusService(
        chain=safety_chain,
        ledger=paper_ledger,
        account_ctx=account_ctx,
        cells=config.cells,
        caps=hg.allocation_cap.caps,
        default_cap=hg.allocation_cap.default_cap,
        env_fallback_cap=allocation_cap,
        buffers=hg.buying_power.buffers,
        default_buffer=hg.buying_power.default_buffer,
        env_fallback_buffer=balance_buffer_usdt,
        phase=config.phase,
        attempts=attempt_recorder,
        trading_state=trading_state,
        kill_switch=kill_switch,
        deployment=(None if spec.is_simulated else {
            "backend_digest": deployment_identity.backend_digest,
            "source_revision": deployment_identity.source_revision,
            "deployment_id": (str(deployment_identity.deployment_id)
                              if deployment_identity.deployment_id else None),
        }),
        readiness=trading_readiness,
        capital=(CapitalStatusReads(authority=capital.capital_authority, lock=capital.scope_lock,
                                    scope=capital_scope, session_factory=session_factory,
                                    clock=now_ms_utc)
                 if capital is not None else None),
    )

    healthz_port_env = os.environ.get("BFX_HEALTHZ_PORT", "").strip()
    healthz_port = int(healthz_port_env) if healthz_port_env else 8080
    healthz_host = os.environ.get("BFX_HEALTHZ_HOST", "0.0.0.0").strip() or "0.0.0.0"
    admin_token = os.environ.get("BFX_ADMIN_TOKEN", "").strip() or None

    # Phase 4.4a Task 19: WS dispatcher — only wired when live executor +
    # BFX_WS_CLIENT_ENABLED=true. Paper path: spec.ws_client_enabled=False
    # → these remain None → run() TaskGroup skips the ws_dispatcher task.
    auth_ws: BitfinexAuthWSClient | None = None
    ws_dispatcher: BitfinexLiveWSDispatcher | None = None
    if spec.ws_client_enabled:
        auth_ws = BitfinexAuthWSClient(
            creds=credentials,
            auth_gate=bfx_auth_gate,
            on_resync_needed=(
                periodic_reconcile.request_resync
                if periodic_reconcile is not None
                else None
            ),
        )
        assert venue_hint_sink is not None  # built above for any WS-enabled executor
        ws_dispatcher = BitfinexLiveWSDispatcher(
            ws_client=auth_ws,
            event_sink=stdout_sink,
            venue_hint_sink=venue_hint_sink,
        )
        bus.subscribe(CancelRequested, ws_dispatcher.handle_cancel_requested)
        # Saturation signal: queue depth read live at scrape time (replaces the
        # 30s ws_dispatcher_queue_depth log line as the primary surface).
        _dispatcher_for_gauge = ws_dispatcher
        metrics.bind_ws_dispatcher_queue(
            depth_fn=lambda: _dispatcher_for_gauge.queue_depth,
            capacity=ws_dispatcher.queue_capacity,
        )
        # OTel span "ws_dispatcher.process" — enabled-only in-place wrap of the
        # per-event translate+persist+publish step (fail-open, transparent).
        if tracing.enabled:
            instrument_ws_dispatcher(ws_dispatcher, tracing=tracing)

    # Funding-book depth self-recording (2026-07-19): Bitfinex serves no
    # historical book — book-aware backtests can only use data we record.
    # Additive/fail-open; default off, flag lives in deploy/vm/canary.env.
    if os.environ.get("BFX_BOOK_SNAPSHOT_ENABLED", "").lower() == "true":
        book_snapshot_writer = BookSnapshotWriter(
            rest=bitfinex,
            session_factory=session_factory,
            symbols=sorted(configured_symbols(config.cells)),
            interval_s=int(os.environ.get("BFX_BOOK_SNAPSHOT_INTERVAL_S", "3600")),
        )

    return Daemon(
        config=config,
        account_bootstrap=account_bootstrap,
        registry=registry,
        candle_q=candle_q,
        diagnostics=diagnostics,
        probe=probe,
        monitor=monitor,
        scheduler=scheduler,
        signal_engine=signal_engine_obj,
        db_engine=db_engine,
        ws_client=ws_client,
        writer=writer,
        bitfinex_http=bitfinex_http,
        bitfinex=bitfinex,
        session_factory=session_factory,
        executor=executor,
        safety_chain=safety_chain,
        account_ctx=account_ctx,
        ledger=paper_ledger,
        bus=bus,
        smoke_runner=smoke_runner,
        fill_tracker=fill_tracker,
        auth_ws=auth_ws,
        ws_dispatcher=ws_dispatcher,
        boot_recovery=boot_recovery,
        observation_scope=capital_scope if boot_recovery is not None else None,
        book_snapshot_writer=book_snapshot_writer,
        interest_ledger_sync=interest_ledger_sync,
        credit_history_sync=credit_history_sync,
        funding_book_service=funding_book_service,
        periodic_reconcile=periodic_reconcile,
        healthz_host=healthz_host,
        healthz_port=healthz_port,
        admin_token=admin_token,
        trading_status=trading_status,
        trading_readiness=trading_readiness,
        writer_lock=writer_lock,
        command_gate=reservation_middleware.command_gate,
        uncertainty_worker=uncertainty_worker,
        metrics=metrics,
        tracing=tracing,
        protection=protection,
        trading_control=trading_control,
        capital_policy_control=capital_policy_control,
        writer_lock_watch=(
            WriterLockWatch(lock=writer_lock)
            if writer_lock is not None else None
        ),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run())
    except WriterLockUnacquired as exc:
        # Another live writer holds the Postgres advisory lock (boot acquire in
        # build_daemon). Fail fast with EX_TEMPFAIL so the platform staggers a
        # retry instead of two processes contending on real-money execution.
        log.critical(
            "writer_lock_unacquired %s — sys.exit(EXIT_CODE_WRITER_LOCKED=75)",
            exc,
        )
        sys.exit(EXIT_CODE_WRITER_LOCKED)
    except ValueError as exc:
        log.error("config_fatal %s — exit 1", exc)
        sys.exit(1)


async def _run() -> None:
    # T8: operator alerts. Installed before anything can refuse the boot; hooks in
    # safety/{trading_state,protection,kill_switch}.py emit into it. Log-only
    # (said once) when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set.
    alerts.install(alerts.AlertSink.from_environment(os.environ))
    try:
        daemon = await build_daemon()

        # Phase 4.4 prework: boot smoke (pre-TaskGroup) — see daemon_smoke_boot.py
        from bfx_funding_bot.modules.marketfeed.daemon_smoke_boot import run_boot_smoke
        await run_boot_smoke(daemon)
    except Exception as exc:
        alerts.emit(alerts.BOOT_REFUSED, error=_error_text([exc]))
        await alerts.shutdown()
        raise
    if daemon.metrics is not None:
        alerts.current().observer = daemon.metrics.observe_alert

    stop = daemon._stop_event  # share with signal handler
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "daemon_started phase=%s cells=%d",
        daemon.config.phase, len(daemon.config.cells),
    )

    # Optional duration cap (BFX_RUN_DURATION_HOURS); shadow has no duration.
    duration = daemon.config.run_duration_hours
    if duration is not None:
        async def _duration_timer() -> None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration * 3600)
            except TimeoutError:
                log.info("daemon_run_duration_reached hours=%d", duration)
                stop.set()
        _timer_task = asyncio.create_task(_duration_timer())  # noqa: RUF006

    try:
        await daemon.run()
        log.info("daemon_run_clean_exit")
    except* asyncio.CancelledError:
        log.info("daemon_cancelled_via_signal")
    except* ExecutorAuthError:
        # Auth failure means credentials are wrong / revoked — operator must
        # intervene. Avoid auto-retry loop (Google SRE Book ch. 22 — auth
        # crash-loop-backoff via sysexits EX_CONFIG 78 lets Koyeb stagger
        # restarts instead of tight crash-on-boot retries.)
        log.critical(
            "executor_auth_failed — sys.exit(EXIT_CODE_AUTH_FAILED=78)",
        )
        alerts.emit(alerts.DAEMON_FATAL if daemon.booted else alerts.BOOT_REFUSED,
                    error="ExecutorAuthError: venue credentials rejected")
        sys.exit(EXIT_CODE_AUTH_FAILED)
    except* Exception as eg:
        log.error(
            "daemon_taskgroup_fatal exceptions=%s",
            [type(e).__name__ for e in eg.exceptions],
        )
        alerts.emit(alerts.DAEMON_FATAL if daemon.booted else alerts.BOOT_REFUSED,
                    error=_error_text(eg.exceptions))
        raise
    finally:
        # Cleanup after TaskGroup completes (close http client)
        log.info("daemon_shutdown_complete")
        # Release the single-writer advisory lock so the next process can acquire
        # it without waiting for the server-side session to expire (live+PG only).
        if daemon.writer_lock is not None:
            with contextlib.suppress(Exception):
                await daemon.writer_lock.release()
        await daemon.bitfinex_http.aclose()
        # SmokeRunner.aclose() is a no-op for PostgresEventLogQueryAdapter (no owned client);
        # kept for forward-compatibility with adapters that may hold resources.
        if daemon.smoke_runner is not None:
            with contextlib.suppress(Exception):
                await daemon.smoke_runner.aclose()
        # Flush any batched spans before exit (no-op when tracing disabled).
        if daemon.tracing is not None:
            with contextlib.suppress(Exception):
                daemon.tracing.shutdown()
        # Deliver queued alerts (a refused boot, a fatal error) before exiting.
        await alerts.shutdown()


def _error_text(exceptions: Sequence[BaseException]) -> str:
    return "; ".join(f"{type(exc).__name__}: {exc}" for exc in exceptions)[:300]



if __name__ == "__main__":
    main()
