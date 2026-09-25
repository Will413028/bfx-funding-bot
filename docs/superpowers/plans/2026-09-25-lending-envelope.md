# Lending envelope refactor — implementation plan

Branch: `refactor/lending-envelope`. Decision: second-brain ADR
`2026-09-25-lending-envelope-replaces-probation-and-account-halt` (supersedes
`2026-09-25-automated-probation-replaces-release-ceremony`). Platform layer only;
per-tenant settings stay in Phase 5. Production keeps running the current build
until the cutover in §4.

## 1. Target behaviour

- **Envelope (D1)**: every per-symbol term lives in the versioned `CapitalPolicy`
  (DB, dry run → digest → TOTP/amend apply): `enabled`, `reserve_amount`,
  `max_offer_amount`, `max_cell_fraction`, `min_period_days`, `max_period_days`,
  `max_open_offers`, `rate_floor_ratio`, `min_rate_apr`. The effective rate floor
  is `max(min_rate_apr / 365, median_bid × rate_floor_ratio)`; below it the symbol
  stays idle. `pre_trade_limits.symbols` leaves `safety*.yaml`. The command
  throttle protects the venue API, not the lending terms, so it stays platform
  config (`pre_trade_limits.command_rate`).
- **Managed offers only (D2)**: an offer or credit is managed iff it traces to a
  durable intent. Anything else is *foreign*: never cancelled or repriced by the
  bot, excluded from managed exposure, its amount already absent from venue
  `available`; it raises a `foreign_exposure` alert once per venue id.
- **UNKNOWN resolution by amount fingerprint (D3a)**: funding submits carry no
  `cid`, so every submitted amount encodes a fingerprint in its last 4 decimals
  (1..9999, unique among the symbol's non-terminal claims/attempts; the amount
  moves by < 0.0001 and stays within [venue minimum, max_offer_amount]). After
  the settle window, a complete snapshot + offer history resolves an UNKNOWN:
  an offer (active or terminal) with the fingerprinted amount, symbol, rate and
  period → claim it; history complete over the submit window and no such offer
  → not sent; incomplete evidence → stays quarantined. Replaces the
  "never match by amount/rate/time" rule.
- **Reaction ladder (D3)**:
  1. reject one action — envelope, book stale, auth DOWN, heartbeat;
  2. symbol quarantine, auto-clearing — UNKNOWN submit, read failure, unaccepted
     snapshot. Derived from open uncertainty (existing per-currency
     `UncertaintyGuard` / command-gate latch); no stored state. Alert when a
     quarantine is older than **30 min**, again every 6 h; never escalates;
  3. account `HALTED/auto` + cancel **managed** offers by id + TOTP resume —
     offer amount mismatch, identity conflict, throttle trip, a managed offer or
     credit outside the envelope, ledger conservation broken and not explained
     by foreign activity (below);
  4. alert only — foreign exposure, NAV drop (old loss/drawdown limiter).
  Writer lock loss → stop writing and exit non-zero (supervisor restarts); no
  trading-state write.
- **Lent above ledger attribution**: on an accepted snapshot, the unexplained
  delta per symbol is first matched against foreign offers that left the book
  since the previous accepted snapshot (offer history status `EXECUTED`/partial,
  summed amount ≥ delta − 0.01). Fully explained → level 4; otherwise level 3.
- **Operator controls (D4)**: trading state is `ACTIVE | HALTED`, cause
  `operator | auto`. Resume = webapi TOTP request, no probation. Kill =
  `HALTED/operator` + venue cancel-all (documented: also cancels manual offers).
  Static token keeps `/admin/halt` only. Per-symbol `enabled=false` makes the
  reconciler cancel that symbol's managed offers (new) and post nothing.
- **Deploys (D5)**: the bot knows nothing about change classes. The deploy tool
  keeps: pending-migration → stop bot → backup → restore test → migrate, the
  `DR_TRIGGER_PATTERNS` restore-test trigger, rollback when no migration ran,
  and the `deployments` ledger (without `change_class`).

## 2. Tasks

Each task lands with regression tests; money-path tasks (T1–T4) need a mutation
check. Commands: `cd backend && uv run pytest -m "not integration"`, affected
integration tests (Docker), `uv run mypy src/`, `uv run ruff check`,
`uv run alembic check`; frontend `pnpm vitest run`, `pnpm tsc --noEmit`,
`pnpm biome check src/`.

| # | Task | Depends | Acceptance |
|---|---|---|---|
| T1 | Envelope into `CapitalPolicy`: new fields + migration + `amend_capital_policy.py`; `PeriodBoundsGuard`/`OpenOfferLimitGuard`/`RateFloorGuard` read the applied policy; absolute `min_rate_apr` floor; drop `pre_trade_limits.symbols` and the live-boot YAML requirement | — | missing policy field → submit rejected; floor = max(abs, relative) tested both sides; mutation on each comparison |
| T2 | Managed-only capital authority + D3a fingerprint: fingerprinted submit amounts; UNKNOWN auto-resolution from snapshot + history;  unattributed / unclassifiable venue exposure becomes `foreign` in the snapshot classifier (`capital_repository.py` 452/459/516/669/761) instead of raising; `foreign_exposure` alert; drop `orphan_quarantined` trip in `boot_recovery.py` | T1 | a manual offer on the account: no halt, no cancel, budget shrinks by its amount; an UNKNOWN resolves to claimed / not-sent / still-open for the three evidence cases; the existing inventory/historical-cycle regressions still pass |
| T3 | Reaction ladder: remove `submit_outcome_unknown`, `orphan_quarantined`, `unattributed_offer`, `unclassifiable_commitment`, `loss_limiter`, `writer_lock_lost` from `protection.TRIGGERS`; quarantine-age alert; level-3 kill cancels managed offers by id (new `KillSwitch` mode), operator kill keeps cancel-all; lent-above-ledger attribution; `LossLimitMonitor` → alert; `WriterLockWatch` → exit; `RealizedLossGuard`/`DrawdownGuard` removed from the chain and `_LIVE_REQUIRED_HARD` | T2 | one test per trigger level; UNKNOWN blocks only its symbol and clears after resolution; replay of the 2026-08-27..09-24 divergences does not halt |
| T4 | Trading state `ACTIVE/HALTED`: migration archives `trading_state` into `release_archive`, recreates it without REDUCING/probation/material_deploy (seed = current state); drop `guard_trading_state_probation`, `deployment_approvals`; `trading_control_requests` actions `resume|kill`; remove probation from `read_capital`/`evaluate_capital`/status; `/admin/pause` removed; `bootstrap_capital.py` and cutover scripts require HALTED or a disabled policy | T3 | rule parity test on migrated PG; resume never starts a probation; `alembic check` clean |
| T5 | Disabled symbol cancels managed offers: reconciler sweep when `policy.enabled` is false | T1 | integration: disable → managed offers cancelled, foreign untouched, credits untouched |
| T6 | Remove change classes: `DeploymentIdentity` class/ledger walk, `apply_deploy_gate`, `BFX_CHANGE_CLASS` (daemon, compose, `compose_policy.IDENTITY`, CI), `deploy/change-class.yaml`, classifier in `change_class.py` (keep the glob helper for `DR_TRIGGER_PATTERNS` or inline it), `--force-material`, ledger `change_class` column | T4 | deploy dry-run and ledger tests green; migration flow tests unchanged |
| T7 | Frontend: panel shows state, resume (TOTP), kill; remove approve/pause/probation/change_class; i18n strings | T4, T6 | vitest + browser check at cutover |
| T8 | Docs: `backend/ARCHITECTURE.md` §6–§8, `docs/runbooks/operations.md`, `deploy.md`, delete the governance parts of `cutover-release-governance.md`, repo `AGENTS.md`/`AGENTS.local.md` lookups | T1–T7 | every command in the runbooks exists; no reference to probation/material/REDUCING |

## 3. Initial envelope values (fUST)

| Field | Value | Why |
|---|---|---|
| `min_rate_apr` | 1.0% | fixtures `fUST_p2_1h`, last 365 d: p1 0.93%, p5 2.28%, p10 2.96% APR; last 90 d p10 1.82%. With 2-day loans, lending low costs little while idling costs yield, so the absolute floor only catches unit bugs and a market-wide collapse; the relative floor keeps doing the pricing work |
| `rate_floor_ratio` | 0.5 | unchanged |
| `min/max_period_days` | 2 / 2 | unchanged |
| `max_open_offers` | 6 | unchanged |
| `max_offer_amount` | 200 | unchanged (applied at the 09-25 cutover) |

## 4. Cutover

1. Merge after full CI; bfx-deploy runs the migration path (stop bot → backup →
   restore test → migrate → start).
2. Amend the fUST policy with the §3 values (dry run → digest → apply).
3. Check `/admin/trading-status` and `/admin/dry-evaluate`: state carried over,
   no probation fields, floor reported as `max(abs, relative)`.
4. Place a manual offer below the venue book in the Bitfinex UI → expect a
   `foreign_exposure` alert, no halt, no cancel; cancel it by hand.
5. Kill from the UI → cancel-all acknowledged → TOTP resume → ACTIVE, no probation.
