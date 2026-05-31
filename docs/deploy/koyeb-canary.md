# Koyeb Canary Deploy Runbook (Phase 4.4 — first real money)

> ⚠️ **這是真錢上線**：`BFX_PHASE=canary` + `BFX_EXECUTOR=bitfinex_live` 會對
> Bitfinex 帳戶真的掛 funding offer。$570 cap、單策略 2 cells。
> 上線前**每一條 Pre-live Gate 都要過**，不要跳。
>
> 用途：shadow 觀察完 → 首次 canary 真錢 → Phase 4 results doc → Phase 5 scale-up。
> 範圍：承接 [`koyeb-paper.md`](./koyeb-paper.md)（paper/shadow）。本檔只寫 canary 新增的部分；
> DATABASE_URL asyncpg 注意點、service 設定、build troubleshooting 沿用 paper runbook。
>
> Specs: [minimal-canary-enablement-design](../superpowers/specs/2026-05-25-minimal-canary-enablement-design.md)
> · [phase4-roadmap-design](../superpowers/specs/2026-05-18-phase4-roadmap-design.md)
> · [venue-reconcile-verify-design](../superpowers/specs/2026-05-25-venue-reconcile-verify-design.md)

> **目前狀態（2026-05-26）**：canary 已於 2026-05-25 22:31 上線（Koyeb deployment `d824ee7a`，
> HEALTHY），**空跑、帳戶未入金** — 零 order_submit / 零 fill / venue offers=0，無財務風險。
> 本 runbook 作為 SOP 持續適用（重新 deploy / 改 config / kill / rollback）。
> ⚠️ 已知問題：空跑時 executor heartbeat stale（>360s）→ `/healthz` fail → daemon 曾於 22:39 重啟一次；
> 入金前建議先確認 heartbeat 邏輯（見 Troubleshooting 最後一列）。

---

## Pre-live Gate（deploy 前逐條確認）

| # | Gate | 怎麼確認 | 狀態 |
|---|---|---|---|
| 1 | **API key 有 submit/cancel scope** | Bitfinex 網站 → API → 該 key 的 permissions，確認 **Funding** 類別有 **write/create + cancel**（不只 read）。2026-05-25 venue reconcile 只驗過 read | ⬜ user action |
| 2 | API key signed round-trip 通 | 跑下方「Scope 驗證」live contract test，確認 HMAC 簽章被 venue 接受、回應能 parse | ⬜ |
| 3 | 帳戶有可放貸 USD 餘額 | Bitfinex funding wallet 有 ≥ $570 USD（cap 上限）| ⬜ user action |
| 4 | shadow 已跑完觀察窗 | 承接 paper/shadow runbook，shadow 2–4 週數據已進 Phase 4.3 calibration | ⬜ |
| 5 | **G2 calibration audit pass** | Phase 4.3 的 G2 門檻校準（~2026-06-10 觀察窗結束後）| ⬜ gate |
| 6 | main 最新 commit 含 canary config | `git log --oneline -1`，且 `git push origin main`（Koyeb 從 origin/main build）| ⬜ |

> Gate 5（G2 audit）是 spec 定的 4.4 前置 gate。Gate 1/3 是只有你能做的 user action。
> 其餘 backtest / paper / shadow / PG SoT / venue reconcile wire+auth 在 2026-05-25 已 ✅。

### Scope 驗證（Gate 2）

只驗 **read** round-trip（offers query），CI 用的 gated live contract test：

```bash
cd backend_py
BFX_API_KEY=<key> BFX_API_SECRET=<secret> BFX_UPDATE_FIXTURE=1 \
  uv run pytest -m integration -k funding_offers --capture=no
```

通過 = HMAC 簽章被接受、wire format 能 parse（見 `tests/external/bitfinex/test_funding_offers_wire.py`）。

> ⚠️ **submit/cancel scope 沒有現成的安全自動化 test**（自動下單有成交風險）。
> 確認 submit/cancel 權限的可靠方式是 Gate 1（看 Bitfinex permission 頁），
> 實證則靠上線後「第一筆 offer 觀察」（見下方 Post-deploy）——
> 若 key 缺 write scope，第一筆 submit 會在 runtime log 噴 venue error。

---

## Koyeb Secrets（canary 新增）

paper/shadow 已建 `bfx-database-url` / `bfx-redis-url`。canary 額外需要 Bitfinex 憑證
（https://app.koyeb.com/secrets）：

| Secret | 內容 |
|---|---|
| `bfx-api-key` | Bitfinex API key（Funding read + **write/cancel** scope）|
| `bfx-api-secret` | Bitfinex API secret |

> Axiom secret（`bfx-axiom-api-key`）**不再需要** — Axiom 已於 Phase 3c retire，
> 改用 PG event store + structured stdout。見「Known gaps」。

---

## Env Vars（canary 全集）

shadow → canary 是**同一個 service 改 env 後 redeploy**。canary 的完整 env：

| Env Var | 值 | 來源/驗證 | 備註 |
|---|---|---|---|
| `BFX_PHASE` | `canary` | `config.py` 接受；daemon invariant 檢查 | ⚠️ 字面值 |
| `BFX_EXECUTOR` | `bitfinex_live` | `registry.py:61`（default `paper`）| **真錢分界**。從 shadow 的 paper 切過來 |
| `BFX_WS_CLIENT_ENABLED` | `true` | `registry.py:64` | live 必須開，否則 `ExecutorConfigError`（stale exposure）|
| `BFX_API_KEY` | `{{secret.bfx-api-key}}` | `registry.py:91` | live 缺則拒啟動 |
| `BFX_API_SECRET` | `{{secret.bfx-api-secret}}` | `registry.py:91` | 同上 |
| `BFX_ALLOCATION_CAP_USDT` | `570` | `daemon.py:682`（default 500）| 全帳戶 exposure 上限，AllocationCapGuard 強制 |
| `BFX_CELLS_YAML` | `/app/configs/cells.canary.yaml` | `config.py:165` | 2 cells（MR fUSD a30/p2）。image 內建（Dockerfile `COPY configs/`）|
| `BFX_SAFETY_CONFIG` | `/app/configs/safety.canary.yaml` | `daemon.py:712`（default `configs/safety.yaml`）| 6 guards 全開，過 canary invariant |
| `DATABASE_URL` | `{{secret.bfx-database-url}}` | 同 paper | ⚠️ 必須 `postgresql+asyncpg://`，見 paper runbook |
| `REDIS_URL` | `{{secret.bfx-redis-url}}` | 同 paper | optional |
| `BFX_KILL_SWITCH` | **不設** | `hard_guards.py:35` | 設 `=true` 即時全停（見 Kill Switch）|
| `BFX_RUN_DURATION_HOURS` | **不設/刪除** | — | canary 持續跑，不自動退 |
| `BFX_RECONCILE_INTERVAL_S` | **不設**（預設 `90`）| `daemon.py`（`BFX_RECONCILE_INTERVAL_S` 讀取）| live-only；強制 venue snapshot 對帳的週期（正確性骨幹計時器）；不設即用預設 |
| `BFX_RESYNC_MIN_INTERVAL_S` | **不設**（預設 `10`）| `daemon.py`（`BFX_RESYNC_MIN_INTERVAL_S` 讀取）| live-only；WS resync 觸發（重連 / seq-gap）後，非週期對帳的 debounce 視窗（秒）；`0` = 不 debounce |

> canary invariant（`assert_canary_guard_invariant`，daemon build 時）：`BFX_PHASE=canary`
> 下若 `safety.canary.yaml` 任一必要 guard（manual_kill / auth_health / heartbeat /
> allocation_cap / realized_loss_24h / drawdown_from_peak）沒開 → config-fatal `ValueError`，
> 拒絕啟動。`safety.canary.yaml` 已全開，正常不會觸發。

---

## Deploy

> ⚠️ **`scripts/deploy-koyeb.sh` 目前不支援 canary**（只接受 `paper|shadow`，且仍帶
> 已 retire 的 Axiom secret/env）。canary 用下方手動 `koyeb` CLI，或先更新 script（見 Known gaps）。

service 已存在（shadow 在跑），用 `service update` 換 env + 觸發 redeploy：

```bash
koyeb service update bfx-funding-bot/marketfeed \
  --env "BFX_PHASE=canary" \
  --env "BFX_EXECUTOR=bitfinex_live" \
  --env "BFX_WS_CLIENT_ENABLED=true" \
  --env 'BFX_API_KEY={{secret.bfx-api-key}}' \
  --env 'BFX_API_SECRET={{secret.bfx-api-secret}}' \
  --env "BFX_ALLOCATION_CAP_USDT=570" \
  --env "BFX_CELLS_YAML=/app/configs/cells.canary.yaml" \
  --env "BFX_SAFETY_CONFIG=/app/configs/safety.canary.yaml" \
  --env 'DATABASE_URL={{secret.bfx-database-url}}' \
  --env 'REDIS_URL={{secret.bfx-redis-url}}' \
  --env '!BFX_RUN_DURATION_HOURS' \
  --env '!AXIOM_API_KEY' \
  --env '!AXIOM_DATASET'
```

> `!VAR` = 刪除既有 env var。清掉 shadow 殘留的 duration 與 Axiom vars。

deploy 後 tail log：

```bash
koyeb service logs bfx-funding-bot/marketfeed -f
```

首 ~60s 預期（alembic + warmup）：
- `alembic upgrade head` output
- `daemon_started phase=canary cells=2`
- **若看到 `config_fatal` / `ValueError: BFX_PHASE=canary requires all safety guards enabled`** → safety config 指錯或 guard 沒開，exit 1（見 Troubleshooting）

---

## Post-deploy 驗證（真錢，要盯）

1. **第一筆 offer 觀察（最關鍵 — 同時實證 submit scope）**
   - runtime log 應看到 live executor 對 Bitfinex 掛單的 `order_submit` / `ORDER_FILL` 結構化 stdout
   - ✅ submit 成功 = key 有 write scope（Gate 1 實證）
   - ❌ 若噴 venue scope/permission error → 立刻 Kill Switch，回去修 API key 權限
2. **AllocationCapGuard 生效** — 確認 open exposure 不超過 $570（log 會有 cap block 訊息若超）
3. **L3 smoke**（PG read-your-writes，確認 execution chain 落地）：
   ```bash
   curl -X POST "https://<service-url>/smoke-test?level=L3" -H "Authorization: Bearer <admin-token>"
   ```
   回 `{"status":"ok","level":"L3"}` = `RESERVATION_CLAIMED` + `ORDER_FILL` 已落 PG event_log。
4. **頭幾小時持續盯** runtime log，確認 loss/drawdown guard 沒異常觸發。

---

## Kill Switch（緊急全停）

`ManualKillGuard` 會 block 所有新 offer（已掛的不會自動撤，視情況手動到 Bitfinex 撤）：

```bash
koyeb service update bfx-funding-bot/marketfeed --env "BFX_KILL_SWITCH=true"
# redeploy 後 ManualKillGuard block 全部下單
```

解除：`--env '!BFX_KILL_SWITCH'` 再 redeploy。

---

## Rollback

| 場景 | 怎麼回 |
|---|---|
| 想立刻停止真錢下單 | Kill Switch（上）；必要時到 Bitfinex 手動撤未成交 offer |
| 退回 shadow（停真錢，續觀察）| `--env "BFX_PHASE=shadow" --env "BFX_EXECUTOR=paper" --env '!BFX_WS_CLIENT_ENABLED'`（paper + ws=true 會 error，務必一起關 ws）+ redeploy |
| 程式碼 bug | 本機 `git revert <commit>` → push → Koyeb auto-build 回上版 |
| canary config 值要改 | safety/cells canary yaml **無 hot reload** — 改檔 → commit → push → redeploy |

---

## Troubleshooting（canary 特有；build/DB 類見 paper runbook）

| 症狀 | 原因 | 處置 |
|---|---|---|
| exit 1 `ValueError: BFX_PHASE=canary requires all safety guards enabled; disabled: [...]` | `BFX_SAFETY_CONFIG` 沒指到 `safety.canary.yaml`，或該檔某 guard 被關 | 確認 env 指 `/app/configs/safety.canary.yaml`；確認 6 guards 全 `enabled: true` |
| exit 1 `ExecutorConfigError: bitfinex_live requires BFX_API_KEY and BFX_API_SECRET` | secret 沒設或沒注入 | 確認 `bfx-api-key`/`bfx-api-secret` secret 存在且 env 用 `{{secret.*}}` |
| exit 1 `ExecutorConfigError: ...without BFX_WS_CLIENT_ENABLED=true` | live 沒開 WS | 補 `BFX_WS_CLIENT_ENABLED=true` |
| exit 1 `unknown BFX_EXECUTOR=...` | executor 值打錯 | 必須字面 `bitfinex_live` |
| log 第一筆 submit 噴 venue permission/scope error | API key 缺 funding write/cancel scope | Kill Switch → Bitfinex 補權限 → 重新 deploy |
| open exposure 想跑兩 cells 並行但被 cap 卡住 | `reference_amount_usdt=150` × cap 570 = 同時只能一筆 | 調低 `cells.canary.yaml` 的 `reference_amount_usdt`（commit→push→redeploy）|
| 空跑（無成交）時 instance 反覆 unhealthy / daemon 重啟 | executor 無交易活動 → heartbeat stale >360s → `executor degraded` → Koyeb `/healthz` fail | **daemon robustness bug**：executor「正常但無活動」被判 degraded。觀測到 2026-05-25 22:39 重啟一次。**已修 2026-05-26**（branch `fix/executor-liveness-health`）：executor/safety_chain 移出 liveness，HeartbeatGuard 改 watch `ws` |
| smoke L2 boot `smoke_boot_failed` CRITICAL + `offer/submit` 回 500 | 帳戶未入金 → 真實 submit 被 venue 拒（daemon 設計上 continues，非 fatal）| 入金後即消失；若入金後仍 500，才是 API scope/簽章問題 |

---

## Known gaps（建議上線前/後清掉）

1. **`scripts/deploy-koyeb.sh` 過時** — 只支援 `paper|shadow`，且仍把 `bfx-axiom-api-key`
   列為必要 secret + 設 `AXIOM_*` env（Axiom 已 Phase 3c retire）。canary 目前只能手動 CLI。
   建議：加 `canary` case（含上方 env 集）+ 移除 Axiom secret/env requirement。
2. **submit/cancel scope 無安全自動化驗證** — 目前靠 Bitfinex permission 頁 + 上線第一筆實證。
   未來可加「掛極高 rate（不會成交）的 offer → 確認 submit 成功 → 立即 cancel」的 gated live test。
3. **`koyeb-paper.md` 部分內容含 Axiom 殘留**（如 Axiom live stream 連結）— 已大致改 stdout，但可再掃一遍。
