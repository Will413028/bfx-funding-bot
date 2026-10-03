---
title: per-currency allocation Phase 2 — key-partitioned per-symbol values/routing/config, fADA dropped, fUSD dark
date: 2026-06-01
status: active
tags: [bfx-funding-bot, decision, per-currency, allocation, event-sourcing, real-money]
related-commits:
  - "b107df6^..228fa85"
---

# per-currency allocation Phase 2 — per-symbol values/routing/config

## Context

Epic goal: support multiple funding currencies (native-unit denominated). **Phase 1** (`merge 6b94b71`, migration `b7c1d2e3f4a5`) already made every *stateful* surface per-symbol via `dict[symbol]` keys (ledger buckets, `position_state` PK, events carry `symbol`, NAV `_nav_by_symbol`). What remained wrong was **VALUES that still collapse across symbols, one constructor-bound route, and the absent per-symbol config maps**. The live canary is fUST-only, so these were *latent* (everything collapses to the one active symbol) — but the code must be correct-or-loud before any 2nd currency's cap opens. Phase 2 closes them in the pre-launch window. v2 spec absorbed a 4-reviewer adversarial review (caught that the cap authority is **two** sites — guards AND the reconciler sizing path — not one).

## Options Considered

- **Architecture** — **A. single-component, key-partitioned** (one executor/reconciler/tracker; fix the per-symbol VALUES on Phase 1's existing KEY shape) **vs B. structural carve** (per-currency `LendingUnit` + `Supervisor`).
- **Denomination** — **native units now** (each currency its own unit) **vs USD-equivalent cap overlay** (needs a price feed).
- **fADA** — **drop entirely** **vs** keep a `caps:{fADA:0}` config entry **vs** add a real fADA cell.
- **fUSD** — **dark (cap=0, configured but no offers)** **vs** take it live alongside fUST now.
- **Symbol-less events** (`ReservationIntent`/`ReservationFailed`) — keep `DEFAULT_RECONCILE_SYMBOL` as their projection fallback **vs** remove it (as the plan text said).

## Decision

- **D1 (arch) = Paradigm A**, key-partitioned. Borrow exactly one structural idea: an **executor-boundary fail-fast invariant** (reject submit whose `decision.symbol` ∉ configured set).
- **D2 (denomination)** = native-unit rename (`size_usdt`→`amount`, cols, config keys) NOW; USD-equivalent overlay DEFER.
- **D3 (currencies)** = fADA **dropped** (no config entry, no cell); fUSD **dark** (`caps:{fUSD:0}`); fUST live `caps:{fUST:<cap>}`. env↔map precedence: **map wins**; boot `assert_caps_invariant` asserts every configured-cell symbol has an explicit entry + `>0` under canary.
- **D4 (NAV-split)** = DEFER per-symbol loss/drawdown split. HARD GATE: must land before a 2nd currency trades.
- **D5 (execution-time)** = KEEP `DEFAULT_RECONCILE_SYMBOL` (plan was wrong to remove it).

## Rationale

- **D1 — A not B:** Phase 1 already paid for the `dict[symbol]` shape; B adds ~120 LOC of unit/supervisor scaffolding *on top of* the same value-fix work. The shared-resource reality forbids B's payoff — one Bitfinex account / WS / writer-lock / nonce sequence are all singletons, so B buys **zero** isolation while adding a "sequential stepping" invariant that is itself a new silent-race landmine. N=1 funded currency (fUSD/fADA dark) makes a per-currency object graph textbook YAGNI. B's headline "mis-route structurally impossible" is achieved at the single choke point with `symbol=decision.symbol` + fail-fast + regression test, **而非** a unit graph. Forcing function to revisit: independent per-currency cadence / fault isolation / separate rate budgets — none exist pre-launch.
- **D2 — native now:** pre-launch is the cheapest window (no prod data to migrate); `*_usdt` naming is a *lie* for fADA. **代價**: must be atomic with the store reads that consume `size_usdt` (fold + projection + upsert), else rebuild breaks. USD overlay deferred to avoid putting a price feed on the real-money hot path.
- **D3 — fADA drop / fUSD dark:** fADA's on-venue volume is too thin to qualify (operator call) — **相較** keeping an inert `caps:{fADA:0}`, full removal is a pure config cleanup (fADA was never a cell, never in src). fUSD stays a first-class *ready* currency but dark because taking it live needs USD funding **and** the deferred NAV-split (D4); shipping it dark (triple-defended: guard cap=0 block + reconciler `cap==0` skip + executor fail-fast + no cell) validates the per-symbol plumbing at zero real-money risk.
- **D4 — defer NAV-split:** the canary `safety.canary.yaml` has loss/drawdown guards ENABLED, but with one active currency the global NAV sum ≡ fUST NAV, so they behave correctly. **不選** splitting now = YAGNI while single-currency; **代價** = a hard gate (a 2nd currency would let profitable fUST mask a losing fADA under the summed NAV — most dangerous for the volatile asset). `_nav_by_symbol` already keyed → split stays additive.
- **D5 — keep the default:** `ReservationIntent`/`ReservationFailed` have **no `symbol` field** (only `size_usdt`); removing `DEFAULT_RECONCILE_SYMBOL` would feed `symbol=None` into the position projection and break it. **代價**: a real cross-symbol fence-touch landmine remains for these two events — gated (breadcrumb in `store.py`: add symbol+amount to them before a 2nd currency trades).

## Result

- Implemented Tasks 5-13 via subagent-driven-development (TDD + spec-compliance + code-quality review per task); Tasks 1-4 + the route fix pre-existed on the worktree. `git log --oneline b107df6..228fa85`.
- **1099 unit / mypy(123) / ruff** green on merged main. Final 3-lens whole-branch review: real-money **SAFE for fUST-only cutover** (fUST path byte-identical via env-fallback to read-once scalars; ~18 existing reconciler tests unedited), cross-symbol correctness **PASS**, completeness **PASS**.
- Merged main `228fa85` (`--no-ff`), worktree/branch cleaned. **✅ DEPLOYED live VM canary 2026-06-01 (sha `3db7a3b`)** — write-gated SSH cutover; verified `effective_cap_per_symbol {fUST:<cap>}` + caps_invariant + `realized` byte-identical + healthz 200 + 0 error; migration was no-op (already applied in Phase 1 cutover). fUSD dark; available~0 → no new offers yet (steady-state, capital in the single active credit).

## Followup

- ~~**Task 14 — live VM cutover**~~ **✅ done 2026-06-01 (sha `3db7a3b`)** — write-gated SSH cutover verified byte-identical (see Result). Migration was no-op (applied in Phase 1 cutover), so no Neon-cred/alembic-drift step was needed.
- **fUSD-live prereqs (hard-gated):** USD funding · per-symbol NAV-split (D4) · `reconcile_observation` + `offer_claims` symbol columns · symbol+amount on `ReservationIntent`/`ReservationFailed` · fUSD cell in `cells.canary.yaml`.
- **Non-blocking:** `auth_rest.py`/`boot_recovery.py` still carry dead `symbol="fUSD"` param defaults (all live callers pass symbol) → small PR to make required.

## Lessons (optional)

### Observations
- **O1:** `Decimal("0")` is falsy → `x or fallback` on a money path silently drops a legit zero; use `is not None`.
- **O2:** controller recon-before-dispatch caught two defects a 4-reviewer plan review missed — an inter-task ordering dependency (Task 8 needs Task 9's tracker param) and a "remove X" instruction that would crash on symbol-less events. Plan is a floor, not a ceiling.
- **O3 (Phase 1 review):** event `symbol` defaulted to `"fUSD"` while the canary runs fUST — live producers (`reservation_emitting`/`ws_dispatcher`/`fill_tracker`/`boot_recovery`) that omitted `symbol=` landed events in the wrong bucket, so the guard reading the fUST bucket was blind to WS events until the ~90s reconcile overwrote it. Tests stayed green because every fixture passed `symbol` explicitly; the adversarial review caught it (Cluster A2). This is why `symbol` later became required, with `None` failing loud.

## Amendment (2026-09-27): Phase 1 基礎與前身 spec 的裁決

壓縮 05-26 USDT spec、05-31 native spec 與 Phase 1 plan 時補記（來源見 Related）：

- **P0（05-26 → 05-31 supersede）**：05-26 版選「靜態 per-currency USDT cap、balance-aware 延後」，不選 balance 推導 cap；05-31 因 fADA（波動資產）進入目標而改原生單位，且把已上線的 balance-aware gate 納入 per-symbol。兩個 operator 鎖定前提：先建基礎、fUSD/fADA 不上真錢；跨幣別 USD-equivalent 上限延後，hard-guard 熱路徑不引入 price feed（即 D2）。
- **P1（symbol 放哪）**：per-offer symbol 掛在 `DecisionPayload`，不掛 `AccountContext`（後者維持 account singleton）；未設定 symbol → `default_cap=0` 擋下；未知餘額的 symbol `_available=0` → BuyingPower 擋下（fail-safe）。熱路徑沒有任何跨 symbol 加總的方法。
- **P2（position_state 遷移）**：PK 改 `(account_id, deployment_environment, symbol)`，手寫 migration `b7c1d2e3f4a5` **drop + recreate、不 backfill**——它是可由 event_log＋reconcile_observation 重建的衍生 snapshot，且當時 pre-launch 無值得保留的資料；不用 autogenerate（PK 變更不可靠）。
- **P3（expand/contract）**：rename 以過渡 kwargs＋read-alias property 加法落地、ledger getter `symbol=None` 回跨 symbol 總和，讓每個 commit 保持綠；清理延後。plan 同時設 real-money guardrail：live producer 全部傳 `symbol=` 之前不得從 Phase 1 中段部署（fixture 都顯式傳 symbol，單元測試看不出，O3 即此坑）。
- **P4（範圍外）**：餘額維持 REST 每 ~90s 輪詢，不解析 WS `wu`；`reconcile_observation` 與 `rebuild_snapshot_from_log` 在 Phase 1 只對單幣別正確（後由 [2026-06-02-fusd-live-enablement-stage1-cutover](2026-06-02-fusd-live-enablement-stage1-cutover.md) 補 symbol 欄位）。
- **已被推翻**：05-31 spec §4.9 原訂 per-currency loss/drawdown 門檻 map；[2026-06-02-fusd-live-enablement-stage1-cutover](2026-06-02-fusd-live-enablement-stage1-cutover.md) D4 改為 per-symbol 指標＋單一 scalar 門檻。

## Review Notes

(2026-12-01 review: confirm Task 14 cutover happened; check whether fUSD went live and the hard-gated prereqs landed.)

2026-09-12 amendment：Halt 2 recovery 沿用 D3 的 fUST-only scope；`fUSD` 維持 dark，未設定 reserve 不推導隱含保留額而視為 `0`。本輪不需要 full-account `fUSD coverage`；fUSD go-live 仍由既有外部 USD funding 與 Stage 2 prereqs separately gate。這次 amendment 不改變 D4 的第二幣別 hard gate。

## Related

- 2026-09-19 amendment：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 更新D3的cap/map/default來源為scoped applied policy與explicit enabled；幣別原生單位、fUST-only、fUSD disabled不變。上方09-12「不需要full-account fUSD coverage」不再適用：enabled控制新增放貸，full-account snapshot仍需觀察所有持倉，不表示啟用fUSD。新設計已核准，runtime切換仍待驗收。

- Original spec/plan: spec `b5f48fc`→`9be2a79`, plan `b107df6`. Parent epic spec `2026-05-31-per-currency-native-allocation-design.md`（原文已不在 repo，本 ADR 即紀錄）.
- 前身與 Phase 1 來源：`2026-05-31-per-currency-native-allocation-design.md`、`2026-05-26-per-currency-allocation-design.md`（標 SUPERSEDED；原版 `5ad5f7d`）、`2026-05-31-per-currency-native-allocation-phase1.md`。Phase 1 merge `6b94b71`（原文已不在 repo，本 ADR 即紀錄）。
