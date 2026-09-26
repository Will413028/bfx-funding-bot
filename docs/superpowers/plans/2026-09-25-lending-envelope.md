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
     offer amount mismatch, identity conflict, throttle trip, ledger
     conservation broken and not explained by foreign activity (below);
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
| T9 | Per-currency `enabled` from the UI (TOTP): `capital_policy_requests` outbox (migration `7d2a9c4e6b13`), `CapitalPolicyRequestWorker` through `capital_amendment`; runtime role may append an enabled-only revision (DB trigger); kill supersedes waiting enables; overview lists each currency's policy and envelope; panel toggle | T4, T5, T7 | migrated-PG role tests (webapi column INSERT only, bot enabled-only write), worker revision/unchanged/rejection/kill-ordering tests, router + panel tests |

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
   Before resuming, list open `unattributed_venue_offer` uncertainties: rows
   left from the retired orphan quarantine still hold their symbol. Resolve
   each (`closed_at_venue` or `accepted_external_exposure`) in the UI.
3. Check `/admin/trading-status` and `/admin/dry-evaluate`: state carried over,
   no probation fields, floor reported as `max(abs, relative)`.
4. Place a manual offer below the venue book in the Bitfinex UI → expect a
   `foreign_exposure` alert, no halt, no cancel; cancel it by hand.
5. Kill from the UI → cancel-all acknowledged → TOTP resume → ACTIVE, no probation.

## 5. Follow-up release

This release is deployed by the previous bfx-deploy (host tooling is installed
from a release only after it deployed, effective the next run). That tool still
injects `BFX_CHANGE_CLASS`, checks every container carries it, and writes
`deployments.change_class` into both ledger rows. So T6 keeps two transitional
pieces: `BFX_CHANGE_CLASS: ${BFX_CHANGE_CLASS:-retired}` in
`deploy/vm/docker-compose.app.yml` (optional, not required by
`compose_policy.py`), and `deployments.change_class` made nullable without its
CHECK (migration `1f6392809120`; the new tool leaves it NULL).

After the first deploy by the new tool (a `deployments` row with
`change_class IS NULL` and outcome `deployed`), ship a follow-up release that
removes `BFX_CHANGE_CLASS` from the compose file and drops the
`deployments.change_class` column (the attempt-pairing trigger
`check_deployment_attempt` must stop naming it in the same migration).

## 6. Carry-over audit (design review 2026-09-26)

Mechanisms kept on purpose, with the condition that reopens each:

| Mechanism | Why it stays | Re-evaluate when |
|---|---|---|
| `max_offer_amount` is a top-level policy field, not part of `OfferEnvelope` | sizing (`allocate_capital`) caps by it independently of the envelope guard; production holds a schema-2 revision that the cutover amend turns into schema 3 | Phase 5 per-tenant settings, or a schema 4 for any other reason |
| "Managed" is decided at two layers: the capital classifier (claims/attempts, event level) and `venue_offer_state.execution_decision_id` (projection level, used by the envelope guard and `ManagedOfferSweep`) | the projection column is derived from the same claims; the two cannot disagree except while a claim is committing (the 120 s action grace) | any change to how claims bind venue ids, or a third consumer |
| `pathspec` dependency in `bfx_deploy.py` for four DR trigger patterns | same matcher semantics as before, pinned in `deploy/vm/ops/uv.lock` | the next edit of `DR_TRIGGER_PATTERNS`: switch to git's own `:(glob)` pathspec |
| Reconciler legacy uncertainty fallbacks (`evaluate_before_sizing` optional, `ledger.is_uncertain`) | paper/shadow adapters without the durable hook still rely on them | paper/shadow retire, or the next reconciler refactor |
| Python pre-check mirroring the trading-state trigger (`validate_transition`) | clearer errors than the database's, and SQLite fixtures; `test_trading_state_rule_parity` keeps them equal | the rule set grows again |

Open decisions handed to Will are in the second-brain project page Pending.
