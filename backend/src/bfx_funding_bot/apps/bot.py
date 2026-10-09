from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
)

from bfx_funding_bot.apps.authority_support import require_ledger_epoch
from bfx_funding_bot.apps.bot_ports import ObservationVenue, build_capital_ports
from bfx_funding_bot.apps.config import CAPITAL_MAX_SNAPSHOT_AGE_MS, load_config
from bfx_funding_bot.apps.venue import VenueSeam, build_venue
from bfx_funding_bot.core.bounded import run_bounded
from bfx_funding_bot.core.database_realm import assert_database_realm
from bfx_funding_bot.core.db import make_async_engine_from_url
from bfx_funding_bot.core.errors import (
    EXIT_CODE_AUTH_FAILED,
    EXIT_CODE_WRITER_LOCKED,
    ExecutorAuthError,
    WriterLockUnacquired,
)
from bfx_funding_bot.core.health import DB_FRESHNESS, MARKET_DATA_FRESHNESS, HealthProbe
from bfx_funding_bot.core.loop_watchdog import LoopWatchdog, loop_watchdog_timeout_s
from bfx_funding_bot.core.schema_head import assert_schema_head
from bfx_funding_bot.core.telemetry import EventType, HealthStatus, HealthTarget, Level
from bfx_funding_bot.core.writer_lock import WriterLock, derive_lock_key
from bfx_funding_bot.external.bitfinex.auth_rest import BitfinexAuthREST
from bfx_funding_bot.external.bitfinex.auth_ws import BitfinexAuthWSClient
from bfx_funding_bot.external.bitfinex.funding_book_ws import FundingBookWSClient
from bfx_funding_bot.external.bitfinex.funding_rules import FundingRules
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
from bfx_funding_bot.modules.execution.deployment.eligibility import ExecutionGate
from bfx_funding_bot.modules.execution.deployment.ladder import ladder_policy_from_env
from bfx_funding_bot.modules.execution.deployment.period_pricing import PeriodPricer
from bfx_funding_bot.modules.execution.deployment.reconciler import DeploymentReconciler
from bfx_funding_bot.modules.execution.deployment.reprice import policy_from_env
from bfx_funding_bot.modules.execution.deployment.standing_quote import StandingQuoteStore
from bfx_funding_bot.modules.execution.deployment.submit_attempt import (
    SubmitAttemptRecorder,
)
from bfx_funding_bot.modules.execution.diagnostics.sink import DiagnosticsSink
from bfx_funding_bot.modules.execution.events import (
    CancelAcknowledged,
    CancelRequested,
    PositionReconciled,
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
    VenueExecutorPort,
)
from bfx_funding_bot.modules.execution.registry import build_executor
from bfx_funding_bot.modules.execution.resync_channel import ResyncChannel
from bfx_funding_bot.modules.execution.safety.chain import SafetyGuardChain
from bfx_funding_bot.modules.execution.safety.config import (
    load_safety_config,
)
from bfx_funding_bot.modules.execution.safety.hard_guards import (
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
from bfx_funding_bot.modules.execution.uncertainty_requests import (
    ResolutionScope,
    UncertaintyResolutionWorker,
)
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
from bfx_funding_bot.modules.marketfeed.boot_wait import wait_for_database
from bfx_funding_bot.modules.marketfeed.candle_writer import CandleWriter
from bfx_funding_bot.modules.marketfeed.daemon import (
    Daemon,
    _DaemonAuditContextFactory,
    _emit_locf_degraded,
    _refuse_live_boot,
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
from bfx_funding_bot.modules.observability.bot_runs import BotRunRecord
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
    venue_seam: VenueSeam | None = None,
    stop_event: asyncio.Event | None = None,
) -> Daemon:
    """Compose one bot process. ``venue_seam`` is for tests: production passes nothing, so
    the simulated venue runs on the live market feed and without injected faults.
    ``stop_event`` becomes the daemon's stop event; ``_run`` passes the one its loop
    watchdog already follows."""
    config = load_config(cells_yaml_path=cells_yaml_path)
    db_engine = make_async_engine_from_url(config.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    # Both venues, in this order, before the credential vault or any client is touched:
    # a database at another schema means this is the wrong build for it (for instance a
    # rollback onto a newer schema); its stamped realm must equal the realm this process
    # runs as (E2); and the capital authority epoch is read once: either venue runs only
    # on the ledger, and the real venue only on an epoch a known writer appended
    # (``apps/authority_support.py``), before any ledger write. Any refusal stops the boot and
    # alerts (``_refuse_live_boot``: alert routing is configuration, the sink prefixes the realm).
    try:
        # A database that is down or still starting delays the boot instead of failing
        # it (a failed boot only restarts into the same outage); other errors refuse.
        await wait_for_database(db_engine)
        async with session_factory() as boot_session:
            await assert_schema_head(boot_session)
            await assert_database_realm(boot_session, config.deployment_environment.value)
            await require_ledger_epoch(boot_session, venue=config.venue)
    except Exception as exc:
        await _refuse_live_boot(exc, config=config, session_factory=session_factory)
        await db_engine.dispose()
        raise
    try:
        async with session_factory() as bootstrap_session:
            account_bootstrap = await load_account_bootstrap(
                bootstrap_session,
                deployment_environment=config.deployment_environment.value,
                allocation_cap_usdt=Decimal("0"),
                phase=config.phase,
            )
    except Exception:
        await db_engine.dispose()
        raise
    # Capital ports: every adapter a consumer binds to is the ledger's (apps/bot_ports.py).
    env_str = config.deployment_environment.value
    capital_scope = Scope(account_bootstrap.exchange_account_id, env_str)
    account_id = account_bootstrap.account_id
    bus = DomainEventBus()
    resync = ResyncChannel()
    capital = build_capital_ports(
        session_factory=session_factory, scope=capital_scope, account_id=account_id,
        bus=bus, resync=resync, clock=now_ms_utc,
        max_snapshot_age_ms=CAPITAL_MAX_SNAPSHOT_AGE_MS,
    )
    uncertainty_reader = capital.uncertainty_reader
    managed_offers = capital.managed_offers
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
    # Dependency freshness this process reports: the public WS's market data (when it
    # runs one) and the database. Each starts not ready until its first beat.
    market_data_dependencies = [] if skip_ws else [MARKET_DATA_FRESHNESS]
    trading_readiness = TradingReadiness(
        on_change=metrics.set_trading_ready,
        dependencies=(*market_data_dependencies, DB_FRESHNESS),
    )
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
    if (
        config.book_max_age_seconds is None
        or config.book_reconcile_interval_seconds is None
        or config.book_max_down_pct is None
    ):
        raise ValueError(
            "the execution policy requires FundingBookService configuration",
        )
    # The venue this process trades against. The simulated venue opens its own log here,
    # on the database that just passed the schema, realm and authority checks; a refusal
    # (wrong realm or epoch, unmigrated, lost append race) stops the boot.
    try:
        venue_wiring = await build_venue(
            config, exchange_account_id=account_bootstrap.exchange_account_id,
            session_factory=session_factory, db_engine=db_engine,
            bitfinex_http=bitfinex_http, bitfinex=bitfinex, clock=now_ms_utc, metrics=metrics,
            seam=venue_seam,
        )
    except Exception as exc:
        await _refuse_live_boot(exc, config=config, session_factory=session_factory)
        await bitfinex_http.aclose()
        await db_engine.dispose()
        raise
    funding_book_service = FundingBookService(
        store=FundingBookStore(max_age_seconds=config.book_max_age_seconds),
        rest=bitfinex,
        ws=FundingBookWSClient(symbols=sorted(configured_symbols(config.cells))),
        symbols=sorted(configured_symbols(config.cells)),
        reconcile_interval_seconds=config.book_reconcile_interval_seconds,
    )
    registry = StrategyRegistry(build_strategy)
    monitor = HealthMonitor(phase=config.phase, event_sink=stdout_sink, probe=probe,
                            readiness=trading_readiness)
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
    credentials = venue_wiring.credentials
    account_ctx = account_bootstrap.to_context(credentials)
    # ONE µs nonce gate shared by every auth client on this single API key (built with
    # the venue: one per process, whatever the venue).
    # Bitfinex nonces are per-key across REST *and* WS, so mixed scales /
    # independent time-based providers get "nonce: small" rejections — that is
    # what left the auth WS flapping — and concurrent signed requests must also
    # ARRIVE in nonce order, so the gate serializes them. See
    # external/bitfinex/nonce.py.
    bfx_auth_gate = venue_wiring.auth_gate

    diagnostics = DiagnosticsSink(
        session_factory=session_factory, account_id=account_id,
        deployment_environment=env_str,
        metrics=metrics,  # bfx_diagnostic_events_total (safety trips / decisions)
    )
    # Safety config — immutable for daemon lifetime. Config change = redeploy.
    safety_cfg_path = Path(
        os.environ.get("BFX_SAFETY_CONFIG", "configs/safety.live.yaml"),
    )
    safety_cfg = load_safety_config(safety_cfg_path)
    assert_live_guard_invariant(config.phase, safety_cfg)
    hg = safety_cfg.hard_guards

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
    # multi-cell uniform-policy refinement tracked in Phase 4.4. The capital
    # policy is account-scoped (not per-cell), so the envelope labels are
    # informational only.
    first_cell = config.cells[0]

    # M2: SafetyConfig.<guard>.enabled is honoured at build time — disabled
    # guards are not constructed (cleaner than relying on internal no-op).
    # Live (real money) cannot boot with a required guard off
    # (assert_live_guard_invariant). Capital limits are the applied
    # CapitalPolicy's, enforced by CapitalPolicyGuard below.

    # bus is the executor's construction dependency, so it is created above.
    # Env-driven via registry (CC4 invariant — WS client required).
    all_symbols = frozenset(configured_symbols(config.cells))
    spec = build_executor(
        event_sink=stdout_sink,
        phase=config.phase,
        strategy=first_cell.strategy,
        configured_symbols=all_symbols,
        cell=first_cell.cell_id,
        http=venue_wiring.auth_http,
        bus=bus,
        auth_gate=bfx_auth_gate,
        clock=now_ms_utc,
    )

    # Single-writer advisory lock (A1). ACQUIRE only on Postgres: sqlite wiring
    # tests construct the object but must never touch a real lock; the boot
    # acquire raises WriterLockUnacquired on contention → propagates to main() →
    # sys.exit(EXIT_CODE_WRITER_LOCKED). It is built here (before the guards block
    # + the Daemon return) so the same variable is in scope at both the
    # guard-append and the return.
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
            # Readiness gate: block POST only when our MARKET VIEW is stale.
            # Watch market-data freshness ("ws_data": a public WS frame seen),
            # not the reactive executor/safety_chain — those are bumped only by
            # trading itself, so watching them self-suppresses trades in quiet
            # markets and was part of the 2026-05-26 canary restart loop. ws_data
            # stays fresh in quiet markets via Daemon._ws_freshness_tick
            # (Bitfinex hb ~15s); a venue outage ages it and blocks here, with
            # no restart. Never seen since boot also blocks, until the first
            # frame. A composition without the public WS (skip_ws, tests only)
            # has no such dependency to watch.
            watched_sub_tasks=market_data_dependencies,
        ))
    guards.append(CapitalPolicyGuard(authority=capital.capital_authority, scope=capital_scope,
                                     clock=now_ms_utc))
    # Always-on offer envelope (fail-closed); the command throttle config is
    # required for the writer too.
    require_pre_trade_limits(safety_cfg.pre_trade_limits)
    guards.extend(build_pre_trade_guards(
        authority=capital.capital_authority, offers=managed_offers, scope=capital_scope,
        session_factory=session_factory, book=funding_book_service, clock=now_ms_utc))
    # Fail-closed single-writer guard. Authoritative per-submit liveness via verify_held().
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
    quote_store = StandingQuoteStore(
        ttl_ms=int(os.environ.get("BFX_QUOTE_TTL_MS", "3900000")),
    )

    executor: VenueExecutorPort = spec.executor

    # 3a-recovery: venue reconciliation against the real venue.
    book_snapshot_writer: BookSnapshotWriter | None = None
    auth_rest = BitfinexAuthREST(http=venue_wiring.auth_http, auth_gate=bfx_auth_gate)
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

    # 3b: cancel lifecycle → diagnostics (CANCEL_AUDIT). Forensic, best-effort.
    bus.subscribe(CancelRequested,     diagnostics.handle_cancel_requested)
    bus.subscribe(CancelAcknowledged,  diagnostics.handle_cancel_acknowledged)
    # The snapshot behind PositionReconciled feeds the NAV tracker (peak + 24h window) behind
    # the NAV-drop alert, wrapped so it reads metrics the tracker already updated.
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
        CancelRequested, CancelAcknowledged, PositionReconciled,
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
        safety_evaluator=safety_chain,
        boundary=capital.command_boundary,
        uncertainty_reader=uncertainty_reader,
        managed_offers=managed_offers,
        clock=now_ms_utc,
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


    # Last-submit-attempt slot read by GET /admin/trading-status; empty reads as
    # "this process has submitted nothing".
    attempt_recorder = SubmitAttemptRecorder()

    # What the deploy tool says this build is; it names every decision's build
    # and the status report's, and never gates trading (lending envelope D5).
    deployment_identity = DeploymentIdentity.from_env(os.environ)
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
        ownership=writer_lock.verify_held,
    )
    capital_policy_control = CapitalPolicyRequestWorker(
        session_factory=session_factory, account_id=UUID(account_id),
        environment=env_str, authority=operator_authorized,
        policy_store=capital.policy_store, scope_lock=capital.scope_lock,
        clock=now_ms_utc,
        ownership=writer_lock.verify_held,
    )
    deployment_reconciler = DeploymentReconciler(
        capital=capital.capital_authority,
        offers=managed_offers,
        uncertainty=uncertainty_reader,
        scope=capital_scope,
        session_factory=session_factory,
        store=quote_store,
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
        resync=resync,
    )

    signal_engine_obj = SignalEngine(
        phase=config.phase,
        event_sink=stdout_sink,
        diagnostics=diagnostics,
        candles_repo=_CandlesRepoBridge(),
        reporter=DivergenceReporter(build_strategy_at_boundary),
        quote_store=quote_store,
        clock=now_ms_utc,
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
    # The boundary comes from the warmup's `now_mts`, not a fresh clock read:
    # warmup left exactly this tick's candle unobserved (warmup_ref_mts), and an
    # hour rolling over between the two would skip a candle instead.
    for cell in config.cells:
        boundary = last_candle_close_mts(timeframe=cell.timeframe, now_ms=now_mts)
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
    # lock and nothing else. The executor cancels through the venue's own client, so
    # on the simulated venue the cancel-all reaches the simulator.
    command_gate = reservation_middleware.command_gate
    kill_switch = KillSwitch(
        trading_state=trading_state,
        session_factory=session_factory,
        ctx=account_ctx,
        configured_symbols=all_symbols,
        venue=executor if isinstance(executor, FundingCancelAllPort) else None,
        writer_lock=writer_lock,
        uncertainty=uncertainty_reader,
        offers=managed_offers,
        quiesce=lambda: command_gate.quiesced(account_id, timeout_s=QUIESCE_TIMEOUT_S),
        clock=now_ms_utc,
    )
    # An automatic stop writes HALTED alone; the planner then pulls managed offers.
    protection.bind(trading_state)
    trading_control.kill_switch = kill_switch  # the operator's kill request
    command_gate.protection = protection
    if safety_cfg.pre_trade_limits is not None:
        command_gate.throttle = build_command_throttle(
            safety_cfg.pre_trade_limits, protection=protection)

    # ---- GET /admin/trading-status + POST /admin/dry-evaluate ----
    # Status uses the same applied policy reader as the planner and command gate.
    trading_status = TradingStatusService(
        chain=safety_chain,
        exposure=CapitalStatusReads(
            authority=capital.capital_authority, lock=capital.scope_lock, scope=capital_scope,
            session_factory=session_factory, clock=now_ms_utc),
        account_ctx=account_ctx,
        cells=config.cells,
        phase=config.phase,
        attempts=attempt_recorder,
        trading_state=trading_state,
        kill_switch=kill_switch,
        deployment={
            "backend_digest": deployment_identity.backend_digest,
            "source_revision": deployment_identity.source_revision,
            "deployment_id": (str(deployment_identity.deployment_id)
                              if deployment_identity.deployment_id else None),
        },
        readiness=trading_readiness,
    )

    healthz_port_env = os.environ.get("BFX_HEALTHZ_PORT", "").strip()
    healthz_port = int(healthz_port_env) if healthz_port_env else 8080
    healthz_host = os.environ.get("BFX_HEALTHZ_HOST", "0.0.0.0").strip() or "0.0.0.0"
    admin_token = os.environ.get("BFX_ADMIN_TOKEN", "").strip() or None

    # Phase 4.4a Task 19: WS dispatcher — wired only for a venue whose capabilities require
    # the authenticated WebSocket (Bitfinex); the simulated venue has none, so these remain
    # None and run() skips the dispatcher task.
    auth_ws: BitfinexAuthWSClient | None = None
    ws_dispatcher: BitfinexLiveWSDispatcher | None = None
    if venue_wiring.capabilities.auth_ws == "required":
        auth_ws = BitfinexAuthWSClient(
            creds=credentials,
            auth_gate=bfx_auth_gate,
            on_resync_needed=resync.request if periodic_reconcile is not None else None,
        )
        ws_dispatcher = BitfinexLiveWSDispatcher(
            ws_client=auth_ws,
            event_sink=stdout_sink,
            venue_hint_sink=capital.venue_hint_sink,
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
        bus=bus,
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
        writer_lock_watch=WriterLockWatch(lock=writer_lock),
        venue_tasks=venue_wiring.tasks,
        venue_aclose=venue_wiring.aclose,
        venue_diagnostics=venue_wiring.simulated,
        _stop_event=stop_event if stop_event is not None else asyncio.Event(),
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
        watchdog = LoopWatchdog(timeout_s=loop_watchdog_timeout_s(os.environ))
    except Exception as exc:
        alerts.emit(alerts.BOOT_REFUSED, error=_error_text([exc]))
        await alerts.shutdown()
        raise
    # Armed before the build, so a loop wedged during the build or the boot observation
    # also ends the process; it follows the daemon's stop event and disarms when a stop
    # is requested, so the graceful drain is never cut short.
    stop = asyncio.Event()
    watchdog_task = asyncio.create_task(watchdog.run(stop), name="loop_watchdog")
    try:
        await _run_daemon(stop, watchdog)
    finally:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task


async def _run_daemon(stop: asyncio.Event, watchdog: LoopWatchdog) -> None:
    try:
        daemon = await build_daemon(stop_event=stop)
    except Exception as exc:
        alerts.emit(alerts.BOOT_REFUSED, error=_error_text([exc]))
        await alerts.shutdown()
        raise
    if daemon.metrics is not None:
        alerts.current().observer = daemon.metrics.observe_alert
        watchdog.on_lag = daemon.metrics.observe_event_loop_lag

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # This run's row in bot_runs, written as the writer (the build acquired the lock).
    # Reports a previous run of the scope that ended without a recorded end, once.
    run_record = BotRunRecord(
        daemon.session_factory,
        exchange_account_id=daemon.account_bootstrap.exchange_account_id,
        deployment_environment=daemon.account_bootstrap.deployment_environment,
    )
    await run_record.start()
    # How this run ends, recorded after the drain. None (a BaseException other than
    # those below) leaves the row open: the next boot reports it, as it does for the
    # exits that never reach here (loop watchdog, OOM kill, SIGKILL).
    end_reason: str | None = None

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
        end_reason = "clean_stop"
    except* asyncio.CancelledError:
        log.info("daemon_cancelled_via_signal")
        end_reason = "clean_stop"
    except* ExecutorAuthError:
        # Auth failure means credentials are wrong / revoked — operator must
        # intervene. Exit with sysexits EX_CONFIG 78 (Google SRE Book ch. 22)
        # so the exit is distinguishable from a crash; restarts are left to
        # the container restart policy (deploy/vm/docker-compose.app.yml).
        log.critical(
            "executor_auth_failed — sys.exit(EXIT_CODE_AUTH_FAILED=78)",
        )
        alerts.emit(alerts.DAEMON_FATAL if daemon.booted else alerts.BOOT_REFUSED,
                    error="ExecutorAuthError: venue credentials rejected")
        end_reason = "fatal" if daemon.booted else "boot_refused"
        sys.exit(EXIT_CODE_AUTH_FAILED)
    except* Exception as eg:
        log.error(
            "daemon_taskgroup_fatal exceptions=%s",
            [type(e).__name__ for e in eg.exceptions],
        )
        alerts.emit(alerts.DAEMON_FATAL if daemon.booted else alerts.BOOT_REFUSED,
                    error=_error_text(eg.exceptions))
        end_reason = "fatal" if daemon.booted else "boot_refused"
        raise
    finally:
        # Cleanup after TaskGroup completes (close http client)
        log.info("daemon_shutdown_complete")
        # The drain is over: record how the run ended while still the writer (before
        # the lock is released). Returns within FINISH_TIMEOUT_S even when the database
        # stops answering (the write is abandoned, not awaited).
        if end_reason is not None:
            await run_record.finish(end_reason)
        # Release the single-writer advisory lock so the next process can acquire
        # it without waiting for the server-side session to expire (PG only). Bounded
        # like the run's end: a database that stopped answering must not hold the exit;
        # the server drops the session lock when the abandoned connection dies.
        if daemon.writer_lock is not None:
            with contextlib.suppress(Exception):
                await run_bounded(daemon.writer_lock.release(), timeout_s=_RELEASE_TIMEOUT_S,
                                  what="writer_lock_release")
        await daemon.bitfinex_http.aclose()
        if daemon.venue_aclose is not None:
            with contextlib.suppress(Exception):
                await daemon.venue_aclose()
        # Flush any batched spans before exit (no-op when tracing disabled).
        if daemon.tracing is not None:
            with contextlib.suppress(Exception):
                daemon.tracing.shutdown()
        # Deliver queued alerts (a refused boot, a fatal error) before exiting.
        await alerts.shutdown()


# Exit-path bounds: the run's end (bot_runs.FINISH_TIMEOUT_S, 5s), the writer-lock release
# (5s) and alerts.shutdown (5s) stay well inside the 30s stop_grace_period of
# deploy/vm/docker-compose.app.yml.
_RELEASE_TIMEOUT_S = 5.0


def _error_text(exceptions: Sequence[BaseException]) -> str:
    return "; ".join(f"{type(exc).__name__}: {exc}" for exc in exceptions)[:300]



if __name__ == "__main__":
    main()
