---
title: fUSD-live enablement prereqs + Stage 1 decoupled cutover
date: 2026-06-02
status: active
tags: [bfx-funding-bot, decision, per-currency, fusd, migration, event-sourcing, deployment]
related-commits:
  - "2c0b7cc"   # NAV-split D4
  - "008146a"   # fUSD prereqs P1+P3+P2
  - "c5951e8"   # foot-gun hardening
  - "e612228"   # runbook fix + Stage 1 cutover
---

# fUSD-live enablement prereqs + Stage 1 decoupled cutover

## Context

Bot trades fUST-only live (VM canary, capped; fUSD dark). The per-currency epic (Phase 1 `6b94b71` + Phase 2 `228fa85`) + NAV-split D4 (`2c0b7cc`) made every stateful/guard surface per-symbol. Before a **2nd** currency (fUSD) can trade, four symbol-blind seams — latent today (everything collapses to the one active symbol) but real-money mis-attribution the instant a 2nd cell trades — had to be closed: `reconcile_observation`/`offer_claims` symbol columns, `ReservationIntent`/`Failed` symbol+amount, and the boot missing-claim release path. The external prereq (USD funding) is operator-owned. This ADR compresses the fUSD-live-enablement spec + NAV-split spec, plus the **Stage 1 cutover** (which decoupled the binary/migration deploy from funding) and the pre-flight that drove two of the decisions.

## Options Considered

- **Legacy symbol-less event evolution (P3):** A. upcast at the deserialize boundary, never touch the log · B. rewrite/migrate the append-only event_log.
- **Missing-claim symbol on boot recovery (P2, riskiest seam):** A. unrepresentable-bad-state (NOT NULL + read `claim.symbol` + fail-loud on ∉ configured) · B. runtime fallback (the old `symbols[0]` hardcode / a default).
- **NAV-split loss/drawdown threshold:** A. per-symbol metrics, single scalar threshold · B. per-symbol threshold map.
- **Stage 1 deploy timing:** A. decouple — deploy per-symbol binary + 2 migrations NOW (fUSD dark), cutover later = config flip · B. bundle the migrations+binary with the fUSD funding event.
- **Rollback of the Stage 1 deploy:** A. code-only (revert binary, leave DB at head, restore migration files so `upgrade head` no-ops) · B. compose-redeploy the old sha `3db7a3b` · C. `alembic downgrade` / Neon PITR.

## Decision

- **D1 (= spec D5)** Upcaster, never rewrite history → A; extended this session to inject `'fUST'` for **all 5** legacy event types + fail-loud `__post_init__` on `symbol=None`.
- **D2 (= spec D7)** Missing-claim symbol = unrepresentable-bad-state → A; killed `symbols[0]`, read `claim.symbol`, fail-loud assert.
- **D3 (= spec D4)** Backfill `server_default='fUST'` on both tables.
- **D4 (NAV-split spec)** Per-symbol metrics, single scalar threshold → A.
- **D5 (this session)** Decouple Stage 1 from funding → A.
- **D6 (this session, pre-flight-surfaced)** Code-only rollback → A.

## Rationale

- **D1:** event-sourcing best practice = schema evolution in the read/deserialize layer; rewriting an append-only log destroys the audit guarantee (anti-pattern even pre-launch). The pre-flight found the upcaster only covered INTENT/FAILED while **419 legacy fUST FILL / 422 CLAIMED** rows lacked symbol → `rebuild_snapshot_from_log` (zero prod callers, but a real future-recovery tool) would silently drop them and miscompute realized. Extending to all 5 + a fail-loud guard closes it.代價：a new fail-loud path (empty-symbol venue `foc` now raises) — accepted (empty symbol is already corrupting).
- **D2:** unrepresentable-bad-state > checked-bad-state > silent fallback. The `symbols[0]` fallback re-introduced the exact mis-attribution P2 exists to remove. 代價：boot/periodic reconcile hard-aborts if a claim's `symbol ∉ configured_symbols` (intended fail-fast — abort > corrupt; never fires fUST-only).
- **D3:** canary is fUST-only (fact, not preference) → DB stamps existing rows unambiguously. 不選 `'fUSD'` (anchored to the stale `symbols[0]`/`ClaimRecord` default) — it would mis-stamp every live row to a never-traded currency.
- **D4:** homogeneous stablecoin funding (no price exposure; NAV only drops on bug / socialised-loss / withdrawal) has no vol difference to calibrate a per-symbol threshold against, and a portfolio same-% guard is mathematically redundant to per-symbol. A per-symbol map would encode a distinction that doesn't exist. 代價：a profitable fUST could mask a losing 2nd currency — accepted for two stablecoins, revisit for a volatile asset.
- **D5:** deploying the inert-for-fUST binary + additive migrations early validates "new binary runs fUST byte-identical" in isolation **and** shrinks the eventual real-money cutover to a pure config flip — the textbook de-risk; migrations are additive + forward-compatible so the early apply is low-risk. 代價：one extra real-money deploy now vs bundling. Revises the runbook's original Stage-1-bundled-with-funding choice.
- **D6:** redeploying `3db7a3b` via compose is **broken** — the old tree's alembic head is `b7c1d2e3f4a5`, lacks `dac1e2f3a4b5`, so the compose `migrate` one-shot `alembic upgrade head` raises `Can't locate revision` → `bot` (depends_on migrate success) never starts = no live writer. Additive columns are forward-compatible, so reverting CODE while leaving the DB at head (restore the 2 migration files into the old tree → `upgrade head` no-ops) is safe. 不選 `alembic downgrade` on a live daemon; Neon PITR (6h window) is corruption-only.

## Result

- Merged: `git log --oneline 3db7a3b..e612228` (NAV-split `2c0b7cc`, fUSD prereqs `008146a`, hardening `c5951e8`, runbook+cutover `e612228`). 1123 unit / mypy / ruff green; 3-lens + independent reviews READY.
- **Stage 1 cutover DEPLOYED 2026-06-02** — Claude SSH write-gated (push → `KILL_SWITCH=true` + stop bot → `deploy-vm.sh canary` applied `b7c1d2e3f4a5 → c9d0e1f2a3b4 → dac1e2f3a4b5` → verify → flip off). VM canary `e612228`, DB head `dac1e2f3a4b5`, fUST realized **byte-identical**, fUSD dark (`effective_cap {fUST:<cap>}`), reconcile `symbols=1 orphans=0`, bot healthy, 0 error/submit/10001. The 5-lens GO/NO-GO pre-flight workflow drove D1's extension + D6.
- **fUSD DRY-RUN armed 2026-06-02（`8483135`，config-only）**：Will 拍板「不入金先做」，把 Stage 2 config（兩份 safety yaml 設小額 `caps.fUSD` + 2 fUSD MR cells）提前部上；USD 未入金 → per-symbol balance gate 擋下每筆 fUSD offer。刻意**不開 KILL_SWITCH**——balance gate 正是要驗的保命機制，開 gate 反而遮蔽。live 驗 `effective_cap {fUST:<cap>,fUSD:<cap>}`、`reconcile symbols=2`、fUSD 決策照跑、0 fUSD submit、fUST byte-identical。入金即自動放貸 ≤min(fUSD cap, available)、無需再部署；revert=`git revert 8483135`。下方 Followup 的 Stage 2 config 已隨此部上；現況以 [2026-06-01-per-currency-allocation-phase-2](2026-06-01-per-currency-allocation-phase-2.md) 的 09-19 amendment 為準。

## Followup

（2026-08-18 已查證：Stage 2 仍 gated on external USD funding，未觸發，非日期逾期；2026-07-18 review 同結論）

- **fUSD go-live = external USD funding → Stage 2 (config flip)**（2026-08-18 已查證：待外部觸發的常備指令，非逾期工作）: add `caps.fUSD>0` to both safety yamls + 2 fUSD cells to `cells.canary.yaml`; boot-abort guard requires configured ⊇ {fUST,fUSD}; binary + migrations already live. Cutover runbook `docs/superpowers/plans/2026-06-02-fusd-cutover.md` — **obsolete**（2026-09-27，見下方判定）.
- Deferred (do **not** add): per-symbol threshold map, portfolio NAV backstop, peak-HWM persistence across restarts, structural per-currency carve, fADA cell.

## Lessons

### Rules

- **R1:** `Rule: 真錢 DB-migration deploy 前，先採樣 live 資料的真實形狀 + 實跑 rollback 路徑（舊 binary tree × 已升級的 DB head）`。code 過單元/整合/3-lens review ≠ deploy 安全；本次 pre-flight 正是這樣抓到 silent-corruption foot-gun（採樣 event_log 才發現 419 筆無-symbol fUST fill）與壞掉的 rollback（舊 sha 經 compose migrate 會 `Can't locate revision`→bot 不啟動）。
- **R2:** `Rule: append-only event log 的 schema evolution 一律走 deserialize 上層 upcaster，且 upcast 必須涵蓋所有會被 replay 的 event type`，不可只補「目前剛好缺的那幾個」——漏掉的會 silently 被 read-path filter 丟掉。

## Amendment (2026-09-27): cutover runbook 已無操作用途

判定：`docs/superpowers/plans/2026-06-02-fusd-cutover.md` 的步驟已**不能照做**，下方 Followup／Related 的「kept」理由不再成立；2026-09-27 owner 確認：runbook 標為 **obsolete**；原文已不在 repo，本 ADR 即紀錄。fUSD 上線需新設計與程式變更。

- 它依賴的東西都已移除：`deploy-vm.sh` 手動部署被 CI digest 部署取代（[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)）；`safety.canary.yaml`／`cells.canary.yaml` 與 canary phase 已刪（`b17e0c4`）；YAML／env 的 `caps` 被 per-symbol applied capital policy 取代（[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D1，fUSD `enabled=false`）；Neon PITR 回滾路徑換成自托 Postgres＋offsite DR。
- 現行程式碼**直接拒絕**啟用 fUSD（`capital_repository` 丟 `unsupported_enabled_symbol`，runbook `docs/runbooks/operations.md` 同述）。所以 fUSD go-live 不再是「入金＋config flip」，而是需要新的程式變更與新決策；Followup 第一條的「待外部觸發的常備指令」同樣失效。
- 仍有價值的部分已在本 ADR：分階段理由（schema→小 cap→ramp，funding 金額 ≠ 風險 cap）、code-only rollback（D6）、boot-abort guard 需 configured ⊇ 實際持倉 symbol、R1/R2。
- 來源：`2026-06-02-fusd-cutover.md`（原文已不在 repo，本 ADR 即紀錄）。

## Related

- Specs compressed here: `2026-06-02-fusd-live-enablement-design.md`、`2026-06-02-nav-split-per-symbol-loss-drawdown-design.md` + plans `2026-06-02-fusd-p{1,3,2}-*.md`、`2026-06-02-nav-split-*.md`.（原文已不在 repo，本 ADR 即紀錄）
- Cutover runbook **obsolete**（2026-09-27 owner）.
- ADR [2026-06-01-per-currency-allocation-phase-2](2026-06-01-per-currency-allocation-phase-2.md).
