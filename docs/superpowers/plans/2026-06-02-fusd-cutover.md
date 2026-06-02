# fUSD go-live cutover runbook

> **Status:** READY — awaiting external USD funding. Do NOT execute any stage until USD is funded + settled in the Bitfinex funding wallet. Every stage is a real-money, write-gated operation — get operator go-ahead before each.

**Goal:** Take fUSD live on the VM canary as a SECOND funding currency alongside fUST, the first time two currencies coexist in production.

**Code state:** P1+P3+P2 (per-symbol checkpoint / events+upcaster / offer_claims+per-claim recovery) + NAV-split D4 are merged to `origin/main` (`a948919`), 1114 unit green, 3-lens READY_TO_MERGE, **UNDEPLOYED**. The live canary still runs older code (~`3db7a3b`, fUST-only). Two un-applied migrations sit on main: `c9d0e1f2a3b4` (reconcile_observation.symbol), `dac1e2f3a4b5` (offer_claims.symbol).

**Deploy mechanics (established):** VM canary, manual `deploy-vm.sh canary` over SSH under a `KILL_SWITCH=true` gate; `git pull` on the VM's `~/bfx-funding-bot`; docker-compose `migrate` one-shot then `bot`. compose does not publish 8080 → verify `/healthz` via `docker exec`. `BFX_SERVICE_VERSION` is hard-set in `~/bfx/bot.env` (cosmetic stale log). ROLLBACK = redeploy the prior sha / Neon PITR (never `alembic downgrade` live). Push to origin needs `gh auth switch Will413028` (already pushed).

---

## Staged plan — why three stages

fUST's first real-money run surfaced bugs only visible with real money (symbol hard-coded fUSD, sci-notation rate, swallowed venue body — memory `live-exec-never-validated`); fUST cap was ramped 450→550→570→3000 for exactly this reason. The fUSD cross-currency path (per-symbol checkpoint, offer_claims.symbol, per-claim recovery, per-symbol NAV) is unit/integration/3-lens validated but **never run on real money with two currencies**. So we separate three independent risk classes:

- **Stage 1 — schema + binary, fUSD still DARK.** Apply both migrations + deploy the per-symbol binary with `caps:{fUSD:0}`. Proves the migrations are clean and fUST stays byte-identical, with zero fUSD exposure. (This is spec §6.5 note #3: deploy the binary BEFORE enabling the fUSD cell.)
- **Stage 2 — fUSD live at a SMALL cap.** Enable 2 fUSD cells + `caps:{fUSD:~400}`. Validate first-ever cross-currency real-money: routing, ledger isolation, guards, NAV-split, recovery — at small size.
- **Stage 3 — ramp the cap.** After ~1 credit cycle (~2 days) clean, raise `fUSD` cap toward ~3000.

**Operator decision (cap):** the operator wants fUSD ≈ fUST (~3000). Recommendation: **fund ~3000 but ramp the cap** per the stages above (funding amount ≠ in-risk cap — the balance-gate keeps actual lending at funded-available; the cap is only the ceiling). Operator may override to set `fUSD:3000` at Stage 2, accepting first-cross-currency real-money at full size. Stages 1→2 are the de-risking; Stage 3 is the ratify-to-scale.

---

## Stage 0 — Pre-conditions (verify all before Stage 1)

- [ ] USD funded and **settled** in the Bitfinex **funding** wallet (not exchange/margin). Confirm available USD balance.
- [ ] `origin/main` is at `a948919` (P1+P3+P2 + NAV-split). Confirm `git -C ~/bfx-funding-bot rev-parse origin/main`.
- [ ] Take a **Neon PITR reference** (note timestamp/snapshot) as the migration rollback point.
- [ ] Confirm the live canary is healthy pre-cutover (273-credit fUST steady state, `/healthz` 200, no restart loop) so any post-deploy delta is attributable.
- [ ] Decide the Stage-2 fUSD cap number (recommended ~400) and the Stage-3 target (~3000).

## Stage 1 — Deploy binary + apply both migrations (fUSD DARK)

No config change to caps/cells yet — `caps:{fUSD:0}` stays. This deploys the per-symbol code + schema only.

- [ ] On the VM: `cd ~/bfx-funding-bot && git fetch && git checkout main && git pull` → at `a948919`.
- [ ] Set the write gate: `KILL_SWITCH=true` (per established cutover).
- [ ] Run `deploy-vm.sh canary`. The compose `migrate` one-shot runs `alembic upgrade head`, applying **in chain order** `b7c1d2e3f4a5 → c9d0e1f2a3b4 → dac1e2f3a4b5` (both `ADD COLUMN NOT NULL DEFAULT 'fUST'`, no table rewrite; advisory-lock serialized).
- [ ] **Verify migrations + schema:** `alembic current` == `dac1e2f3a4b5`; `alembic check` clean; `reconcile_observation.symbol` + `offer_claims.symbol` columns present (psql `\d`), backfilled `'fUST'`.
- [ ] **Verify fUST byte-identical:** position_state fUST `realized` == the pre-cutover value (byte-identical), `effective_cap_per_symbol {fUST:3000, fUSD:0}`, `assert_caps_invariant` passes (fUSD=0 allowed dark), `/healthz` 200 (`docker exec`), 0 errors, no submit/10001 spike.
- [ ] Flip `KILL_SWITCH=false`, restore `bot.env`, recreate. Confirm fUST steady state resumes (273 credit, 0 new offers if available~0).
- [ ] **Hold here** for a short soak (hours) to confirm the per-symbol binary runs fUST-only with zero regression before introducing fUSD.

## Stage 2 — Enable fUSD at a small cap

Config diffs (apply at execution, in BOTH safety files + the canary cells file):

`backend_py/configs/cells.canary.yaml` — add the 2 fUSD MeanReversion cells mirroring the fUST set (a30 + p2; exclude p30-sparse and all RatePercentile, same disqualification as fUST):
```yaml
  - strategy: mean_reversion
    symbol: fUSD
    period_agg: a30
    timeframe: 1h
    params: {threshold_sigma: 0.5, ratio_sigma: 0.3766563427150627, ema_span: 24}

  - strategy: mean_reversion
    symbol: fUSD
    period_agg: p2
    timeframe: 1h
    params: {threshold_sigma: 0.5, ratio_sigma: 0.3450137927640065, ema_span: 24}
```

`backend_py/configs/safety.canary.yaml` AND `backend_py/configs/safety.yaml` — set the fUSD cap (start small):
```yaml
    caps: {fUSD: 400, fUST: 3000}   # fUSD live at small canary cap; ramp later
    # buffers: {fUSD: 3, fUST: 3}   # already correct — no change
```

- [ ] **Pre-flight the boot-abort guard:** `configured_symbols` is derived from the cells; with both fUSD cells added, the configured set is `{fUST, fUSD}` BEFORE any fUSD claim can be recovered — required or `compute_recovery_actions` fail-loud halts boot/periodic reconcile (spec §6.5 note #4). Adding the cells in the same deploy as the cap satisfies this.
- [ ] Commit the config diffs to main, push (`gh auth switch Will413028`).
- [ ] On the VM: `git pull`; `KILL_SWITCH=true`; `deploy-vm.sh canary` (no new migration → `alembic upgrade head` is a no-op).
- [ ] **Verify configured + caps:** `effective_cap_per_symbol {fUST:3000, fUSD:400}`; `assert_caps_invariant` passes (both configured symbols >0 under canary); boot reconcile runs both symbols without the fail-loud firing.
- [ ] Flip `KILL_SWITCH=false`; recreate.
- [ ] **Watch the first fUSD offers (real money):** confirm a fUSD `ORDER_SUBMIT` → fill → fUSD credit; `position_state` has an independent `fUSD` row (reserved/realized in native USD, NOT summed with fUST); AllocationCap/BuyingPower guards read `caps[fUSD]`; per-symbol NAV loss/drawdown guards key on fUSD; no fUST↔fUSD cross-attribution in the ledger; offer_claims rows carry `symbol='fUSD'`. Watch Loki `bfx-submit-fail` + boot alerts for the first fUSD boot.
- [ ] Soak ~1 credit cycle (~2 days). Confirm reconcile recognizes both currencies' credits, no drift, no spurious release/fail.

## Stage 3 — Ramp the fUSD cap

- [ ] After Stage 2 validated clean for a full cycle, raise `fUSD` cap (e.g. 400 → 1000 → 3000) in both safety files; commit/push/redeploy each bump (no migration). Verify `effective_cap_per_symbol` reflects each bump and the balance-gate keeps actual lending ≤ funded-available.

---

## Rollback

- **Stage 1 (schema):** redeploy the prior sha (`3db7a3b`). The new columns are additive + `server_default='fUST'`, so old code keeps running against the upgraded schema (forward-compatible). Only use Neon PITR if a migration genuinely corrupted data — never `alembic downgrade` on the live DB while a daemon runs.
- **Stage 2/3 (fUSD live):** set `caps:{fUSD:0}` (dark) + remove the fUSD cells, redeploy — fUSD stops taking new offers; existing fUSD credits run to maturity. Reverts to fUST-only behaviour.

## Notes
- Do NOT commit the Stage-2 config diffs (fUSD cap>0 / cells) until executing — a stray deploy would enable fUSD before funding/validation.
- `ClaimRecord.symbol='fUSD'` dataclass default (registry_offers.py:70) is dead (from_snapshot always sets it) — cosmetic, out of scope.
- Source of truth for the runbook rationale: spec `2026-06-02-fusd-live-enablement-design.md` §6.5.
