# Minimal Canary Enablement — Design

**Date:** 2026-05-25
**Status:** Approved (brainstorming)
**Context:** Strategy passed backtest (WFO Phase 3b) + paper + shadow; venue
reconcile wire/auth verified (2026-05-25); PG SoT + durable staleness in place.
The remaining blocker to first real money is that `BFX_PHASE=canary` is
explicitly rejected by `load_config` (`config.py:111`) and the
"all-hard-guards-on" invariant the canary needs is unimplemented
(`daemon.py:701` TODO). This spec enables a **minimal, safe** canary phase.

## Goal

Make `BFX_PHASE=canary` a valid, safe real-money phase:
1. Allow `canary` in config.
2. Enforce that real money cannot run with required safety guards silently off
   (refuse to start otherwise).

Deliberately **minimal**: do NOT build the institutional G3 P&L tracking-error
auto-halt gate. For a solo $150 first canary, the existing loss/drawdown guards +
allocation cap + manual monitoring + kill switch are the industry MVP risk stack;
the automated tracking-error halt is a scale-up control, not a launch gate
(avoids premature validation).

## Non-Goals (deferred to scale-up)

- G3 P&L tracking-error auto-halt + real `PnLLedger` aggregation
  (`daemon.py:564` "4.4 wires real PnLLedger" — out of scope here).
- Automated capital ramp.
- `divergence_rate` as a mandatory canary gate (stays optional/calibrated).
- Any per-cell strategy re-selection logic (cell selection is cells.yaml config).

## Background facts (verified 2026-05-25)

- **Phase and executor are orthogonal.** Real execution is driven by
  `BFX_EXECUTOR=bitfinex_live` (+ `BFX_WS_CLIENT_ENABLED=true`), already wired
  (Phase 4.4a/b). `is_simulated` follows the executor, not the phase.
  `Phase.CANARY` has no execution branch anywhere in `src/` except the
  `config.py:111` rejection — it is purely a lifecycle/observability label.
- **Guards are built per-config** (`daemon.py:702-735`): each guard is appended
  only `if <cfg>.enabled` (Phase 4.2 M2). So config can silently disable any
  guard — the footgun this spec closes for real money.
- **Allocation cap** = env `BFX_ALLOCATION_CAP_USDT` (default 500),
  enforced by `AllocationCapGuard` (blocks POST when exposure+offer > cap).
- **Kill switch** = env `BFX_KILL_SWITCH=true` → `ManualKillGuard` blocks all.

## Code changes (2, both small)

### 1. Allow `canary` phase — `config.py` `load_config`

Currently (`config.py:107-113`):
- `phase_str == "canary"` → raises "not allowed in 4.1 daemon".
- `phase_str not in {"paper", "shadow"}` → raises.

Change: accept `canary` as a valid phase. Net effect — valid set becomes
`{paper, shadow, canary}`; unknown values still rejected; empty still rejected.
Update the `phase` field description (`config.py:88`) to drop "canary rejected".

### 2. Canary all-guards invariant — `daemon.py` `build_daemon`

At the guard-building block (`daemon.py:~700`, where both `config.phase` and
`safety_cfg` are in scope — the location flagged by the M2 TODO at line 701):

When `config.phase == Phase.CANARY`, the following guards MUST be enabled in
`safety_cfg`; if any is disabled, raise a config-fatal `ValueError` (propagates
to non-zero startup exit, mirroring existing `load_config` validation failures —
Koyeb will not run real money with a guard off):

- hard: `manual_kill`, `auth_health`, `heartbeat`, `allocation_cap`
- loss-limiters (calibrated): `realized_loss_24h`, `drawdown_from_peak`

`divergence_rate` stays optional. The check runs before/at chain construction.
Paper and shadow phases are unaffected — they may still disable guards.

The invariant only needs to assert `enabled` — the existing `SafetyConfig`
validators (`config.py:52-53`, `64-65`) already guarantee that an enabled
loss-limiter has a non-null threshold, so no separate threshold check is needed.

**Placement rationale:** the invariant couples the marketfeed phase with the
safety config; `build_daemon` is where both are available. `config.py` only sees
the phase string, not `safety_cfg`, so the check cannot live there.

No other code changes — canary needs no execution branch.

## Operational canary profile (deploy-time env/config — NOT code)

Documented here as the recommended launch recipe; operator-set, tunable, rampable:

- `BFX_PHASE=canary`
- `BFX_EXECUTOR=bitfinex_live`, `BFX_WS_CLIENT_ENABLED=true`
- `BFX_ALLOCATION_CAP_USDT=150`
- `safety_cfg`: all 6 required guards `enabled: true`;
  `realized_loss_24h.threshold_usdt ≈ 15`, `drawdown_from_peak.threshold_pct ≈ 15`
- `BFX_KILL_SWITCH` unset (set `=true` to halt instantly)
- `cells.yaml`: only MeanReversion `fUSD×a30` + `fUSD×p2` enabled
  (highest WFO margin 0.199 / 0.188; non-sparse, LOCF-unaffected)
- Koyeb: real `BFX_API_KEY` / `BFX_API_SECRET`

## Prerequisite (user action — gates deploy, not code)

- Confirm the Bitfinex API key has **submit/cancel funding-offer** scope (the
  2026-05-25 reconcile verification only exercised read).
- Account has lendable USD balance.

## Testing (per CLAUDE.md: every change ships with unit tests)

1. `config.py`: `load_config` accepts `BFX_PHASE=canary` and returns
   `phase == Phase.CANARY`; still raises on unknown phase; still raises on empty.
2. `daemon.py` invariant (unit, using the existing `build_daemon` /
   `SafetyConfig` test seams):
   - phase=canary + every required guard enabled → builds without error.
   - phase=canary + each required guard individually disabled → raises
     config-fatal `ValueError` naming the missing guard.
   - phase=shadow + a guard disabled → still builds (invariant canary-only).

## Success criteria

- The daemon boots under `phase=canary` only when all 6 required guards are
  enabled; refuses to start (non-zero exit) otherwise.
- `paper` / `shadow` behaviour is unchanged.
- No execution-path behaviour changes; live execution remains gated solely by
  `BFX_EXECUTOR=bitfinex_live`.
