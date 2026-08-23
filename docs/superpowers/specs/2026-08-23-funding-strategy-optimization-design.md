# Funding Strategy Execution Integrity & Rate Optimization Design

**Date:** 2026-08-23
**Status:** Architecture and implementation spec approved
**Scope:** repository changes only; no remote deployment and no live canary parameter changes

## Goal

Improve expected net lending return while making every live submit decision provably safe, period-correct, model-aware, and auditable. The implementation must remove hidden fallback paths rather than add another compatibility layer.

## Current problem

The active Python backend has five connected gaps:

1. `run_backtest()` supports an injected empirical fill model, but the WFO matrix and fixed OOS helpers do not require or consistently pass one. Missing or low-confidence evidence can silently become a linear estimate.
2. The live clamp consumes scalar funding ticker bid/ask values even though Bitfinex funding-book levels are keyed by `rate`, `period`, `count`, and `amount`. A scalar best ask cannot prove competition for the quote's exact duration.
3. The execution path can turn book-data failure into the original quote. That makes data unavailability indistinguishable from a valid pricing decision.
4. The existing `diagnostics` table is explicitly best-effort and drops unknown operational events. It is useful forensic telemetry, but it is not a durable record of whether a candidate was allowed to reach the venue.
5. The shadow optimizer has no type-level boundary preventing a future caller from accidentally submitting an unavailable or unvalidated recommendation.

The repository already has the right high-level shape—standing quotes, one deployment writer, event-sourced financial state, and a fail-closed safety chain. This design closes the missing boundary between a quote candidate and a venue submission.

## Design principles

1. **No implicit fallback:** missing, stale, mismatched, low-confidence, or invalid evidence produces an explicit blocked/incomplete result. It never reuses the original quote, a global ticker value, or a linear estimate automatically.
2. **Make illegal submission states unrepresentable:** the executor accepts only a validated `ReadyToSubmit` value, never a raw candidate or arbitrary `DecisionPayload`.
3. **Separate financial SoT from execution audit:** `event_log` remains the source of truth for capital/exposure state; a dedicated append-only execution-decision record captures pre-trade eligibility without entering ledger projections.
4. **Logs are observability, not audit:** structured logs and metrics make failures visible; durable database records make decisions reconstructable after restart.
5. **One policy, explicit modes:** replace interacting boolean flags with a validated execution-policy enum. A live policy must declare its required data dependencies at startup.

## 1. Execution eligibility boundary

The deployment flow becomes:

```text
StandingQuote
    + exact-period MarketSnapshot
    + validated FillModelEvidence
    + SafetyGuardResult
        -> ExecutionEligibility.evaluate()
             ├── READY   -> persist ExecutionDecision -> submit ReadyToSubmit
             └── BLOCKED -> persist ExecutionDecision -> no venue call
```

### 1.1 Typed outcomes

Introduce a small execution-domain contract with immutable values:

- `ReadyToSubmit`: contains the final rate, amount, duration, symbol, decision id, market snapshot reference, policy version, and safety result.
- `BlockedExecution`: contains a stable `reason_code`, the candidate identity, failed dependency, and evidence summary.
- `NoRecommendation`: research/observe-only outcome when the optimizer is not armed or lacks evidence; it is not a submit fallback.

The only method allowed to call the venue executor is a method receiving `ReadyToSubmit`. `DeploymentReconciler` may allocate a candidate, but it cannot construct a venue submission directly.

The stable block reasons are:

- `book_fetch_failed`
- `book_not_initialized`
- `book_stale`
- `book_sequence_invalid`
- `book_checksum_invalid`
- `period_not_found`
- `insufficient_period_depth`
- `fill_model_missing`
- `fill_model_low_confidence`
- `optimizer_unavailable`
- `safety_guard_blocked`
- `execution_audit_unavailable`

There is no `FALLBACK` branch and no block reason whose behavior is “use the original quote”.

### 1.2 Policy modes

Use one explicit `execution_policy` rather than independent clamp/optimizer booleans:

- `paper`: no venue submission; may run explicitly named research baselines.
- `book_guarded`: live-capable policy; exact-period market data is mandatory before any submit.
- `optimizer_shadow`: live-capable book-guarded policy plus optimizer observation; optimizer output cannot change the submitted rate.
- `optimizer_live`: future armed mode; empirical model and all optimizer dependencies are mandatory. Missing evidence blocks the candidate.

Startup validation rejects incompatible combinations. A canary process cannot boot with a policy that permits live submission without the book gate. Historical linear scoring is available only through an explicit offline baseline command and is never a runtime dependency fallback.

## 2. Period-correct market data

Bitfinex's funding book exposes duration on each level. The execution policy therefore consumes a typed `MarketSnapshot`, not a `FundingTicker` scalar.

### 2.1 Data-source architecture

- Public funding-book WebSocket snapshot + updates is the primary low-latency source.
- REST funding-book snapshots periodically reconcile the in-memory book and recover from stream gaps/reconnects.
- A checksum/sequence invariant marks the snapshot invalid until the book is resynchronised.
- The existing `funding_book_snapshots` table remains the historical source for self-collected research data, but live eligibility uses a current in-memory snapshot with an explicit captured timestamp.
- Ticker bid/ask remains optional telemetry and cannot satisfy an exact-period book requirement.

### 2.2 Snapshot validity

A snapshot is eligible for a candidate only when all of the following hold:

- the stream has completed an initial snapshot;
- the snapshot age is within the configured `max_age_seconds`;
- the sequence/checksum state is valid or has completed REST reconciliation;
- the requested symbol matches the snapshot symbol;
- an exact `period_days` level exists for the candidate;
- the required side and absolute depth are sufficient for the candidate amount.

Any error that leaves no valid fresh snapshot, stale snapshot, missing exact period, invalid sequence/checksum state, or insufficient depth produces a blocked decision. A REST backstop error does not block by itself when the independently validated WebSocket snapshot is still fresh. The previous quote is not reused.

## 3. Empirical fill model and WFO/OOS

### 3.1 Model contract

The empirical model becomes an explicit artifact with:

- `symbol` and `period_agg` scope;
- fill horizon;
- sample count and confidence status;
- training time range/cutoff;
- schema/model version;
- deterministic artifact hash.

Lookup returns either a high-confidence estimate with its evidence metadata or a typed unavailable result. It must not call `compute_fill_prob()` as a hidden fallback.

### 3.2 Backtest behavior

Thread the required model/config through:

- `run_cell_wfo()` baseline, train, and test runs;
- `evaluate_oos_windows()` strategy and baseline runs;
- `derive_cell_params()` parameter derivation;
- the phase-3b matrix script.

In empirical mode, a missing/low-confidence bucket marks the affected window/cell `INCOMPLETE`; the aggregate report must not present a recommendation based on partial evidence. The report includes model kind, version/hash, data cutoff, sample counts, and incomplete reasons.

An explicit `linear-baseline` research command may remain for historical comparison. Its output is always labeled as a baseline and is not selectable by live policy or used as automatic fallback.

## 4. Rate optimizer and pricing policy

The optimizer evaluates a deterministic candidate set for one quote:

- strategy signal rate;
- exact-period maker price when available;
- exact-period taker price when available and depth is sufficient.

For a validated empirical estimate, score:

```text
expected_net_daily_rate = candidate_rate × fill_probability × (1 - fee_rate)
```

Candidates below the strategy's signal floor are excluded. Ties prefer higher fill probability, then lower quote rate.

`optimizer_shadow` records the selected candidate and the active candidate without changing the submitted rate. If the optimizer is ever armed, unavailable evidence blocks the candidate; it does not fall back to the signal rate. Observe-only mode is not a fallback because the optimizer is explicitly outside the execution authority.

## 5. Durable execution audit

Add an append-only `execution_decisions` table. It is not a ledger projection and does not change capital state.

Each allocation candidate produces exactly one durable decision record before a venue call, with:

- `decision_id`, `reconcile_id`, account/environment, cell, symbol, and signal correlation id;
- outcome: `ready`, `blocked`, or `no_recommendation`;
- stable `reason_code` and failed dependency;
- signal rate, applied rate if any, amount, and duration;
- market snapshot id/hash, captured timestamp, source, and freshness;
- fill-model version/hash and evidence summary when applicable;
- safety policy/result and selected execution policy;
- service version, config hash, occurred timestamp, and recorded timestamp.

For a `ready` result, the audit row must commit before `ReadyToSubmit` reaches the executor. The resulting `ReservationIntent` links back to `decision_id`. If the audit write fails, the candidate is blocked and no venue request is made. The local structured log records the audit failure for diagnosis.

The full raw order book is not duplicated into every decision row. The decision stores a snapshot reference/hash and the relevant exact-period evidence; the immutable snapshot table stores the full payload.

Execution decisions are retained for 365 days by default. Existing best-effort `diagnostics` retention remains separate and shorter.

## 6. Observability and operator state

### 6.1 Structured events/logs

Emit fixed event names with structured attributes:

- `funding.execution.eligibility`
- `funding.execution.blocked`
- `funding.execution.submitted`
- `funding.book.snapshot_invalid`
- `funding.fill_model.unavailable`

Each event includes `decision_id`/`reconcile_id`, symbol/cell, policy, outcome, reason code, timestamp, service version, and relevant bounded evidence. No API keys, secrets, or unbounded exception text are emitted.

Expected data blocks are `WARN`; invariant violations, audit persistence failures, or unexpected exceptions are `ERROR`/`CRITICAL` according to the failure boundary.

### 6.2 Metrics

Add bounded-cardinality metrics:

- `bfx_execution_decisions_total{outcome,reason,policy}`;
- `bfx_execution_audit_persist_failures_total`;
- `bfx_book_snapshot_events_total{result}`;
- `bfx_book_snapshot_age_seconds`;
- `bfx_execution_gate_duration_seconds`;
- `bfx_trading_ready`.

Detailed cell, symbol, model hash, and book evidence remain in logs/audit, not Prometheus labels.

### 6.3 Liveness versus trading readiness

`/healthz` continues to represent process liveness. Market-data or model unavailability sets `trading_ready=0` and appears in admin status/metrics, but does not trigger a restart loop. Continuous blocks are alertable through metrics and logs.

## 7. AdaptivePeriod p14 shadow profile

Retain the locked `AdaptivePeriodStrategy` p14 shadow profile (`p_mid=7`, `p_long=14`, `t1=0.5`, `t2=1.5`) as an opt-in research/deployment profile. It must keep `BFX_PHASE=shadow` and `BFX_DEPLOYMENT_ENV=shadow`, and cannot be selected by a canary configuration.

The profile is evaluated only after the execution-integrity foundation is in place; it does not bypass the book gate or model contract.

## Data flow

```text
Bitfinex WS book ──┐
Bitfinex REST ─────┴─► reconciled MarketSnapshot ─┐
                                                  │
StandingQuote ────────────────────────────────────┼─► Eligibility Gate
Empirical FillModel ──────────────────────────────┘       │
                                                           ├─ BLOCKED → audit + log + metric
                                                           └─ READY → audit → executor → venue
```

## Safety and rollout

- No remote command, container restart, deploy, cap change, rate change, or resume call is part of this implementation.
- `cells.canary.yaml` and `safety.canary.yaml` remain unchanged.
- New live policies fail closed on every missing/invalid dependency.
- Audit persistence failure blocks submission.
- No old quote fallback, ticker-to-period inference, or linear runtime fallback remains.
- The first runtime validation is paper/shadow with zero venue authority; subsequent promotion requires shadow evidence, L3 stability, corrected two-series L4 review, and an explicit operator decision.

## Verification

Unit, integration, and contract tests must cover:

- typed gate invariant: executor is never called for any blocked outcome;
- exact-period book selection, stale data, missing period, insufficient depth, invalid sequence/checksum, reconnect, and REST reconciliation;
- no ticker/original-quote fallback after book failure;
- durable `execution_decisions` fields, ordering before submit, link to `ReservationIntent`, and audit-write failure blocking submission;
- structured event names, bounded metric labels, and trading-readiness state;
- empirical model propagation into WFO train/test/baseline;
- per-symbol model isolation and incomplete windows without aggregate recommendation;
- explicit linear baseline labeling and rejection by live policy;
- optimizer candidate filtering, expected-net-rate selection, fee application, and observe-only invariance;
- `shadow-p14` config selection and canary profile exclusion.

Run targeted tests first, then:

```bash
cd backend_py
uv run pytest -m "not integration"
uv run mypy src/
uv run ruff check
uv run alembic check
```

## Non-goals

- No FRR floor promotion.
- No fixed weekend or spike premium.
- No automatic cap increase.
- No live activation of AdaptivePeriod.
- No historical book reconstruction beyond self-recorded snapshots.
- No remote deployment in this change.
