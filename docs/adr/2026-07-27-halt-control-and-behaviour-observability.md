---
title: 停機控制持久化到 DB，可觀測性改為「查行為」而非「查配置」
date: 2026-07-27
status: active
tags: [bfx-funding-bot, decision, observability, safety, operations]
---

# 停機控制持久化到 DB，可觀測性改為「查行為」而非「查配置」

## Context

2026-07-27 為了暫停真錢放貸而設 `BFX_ALLOCATION_CAP_USDT=0`（`3ad142c`），**它綁不到任何
configured symbol**——那只是 symbol 缺席於 `safety.canary.yaml` `caps` 時的 fallback，而
fUST/fUSD 都明列其中。bot 續放貸數小時（3 筆完整 `INTENT→CLAIMED→ORDER_FILL`），由 operator
質疑「為何三小時前還有下單」才揭穿。當時的「驗證」是 `docker exec printenv` 讀回 0 ——
**驗證輸入而非結果**，該檢查在「生效」與「完全無作用」下輸出相同。

改用真正有效的 `BFX_KILL_SWITCH` 後仍**驗證不了**：錢包可用餘額遠低於 venue 最低下單額，reconciler
size 不出 offer，於是「沒有新單」同時相容於 halt 有效與失效。

Prior state：daemon 自 Phase 4.2 起只暴露**輸入**（env var、yaml、log 行），guard chain
雖已完整且會 emit `deployment_skip guard=... reason=...`，但那條線索只進 log，沒有任何介面
把它變成可查詢的狀態。halt 則自始活在 `deploy/vm/canary.env`。

## Options Considered

### 可觀測性形式
- **A. 加更多結構化 log**：把 guard 判定與 effective config 寫進 log。
- **B. 擴充 `/metrics`**：加 halted / effective_cap 等 gauge。
- **C. 查行為的 admin 端點（選用）**：直接回答「現在會不會下單、被誰擋」。

### halt 狀態存放
- **A. env var（現狀）**、**B. VM 上的檔案**、**C. DB 表（選用）**。

### halt 表形狀
- **A. 單列 upsert**（current state only）、**B. append-only 轉換記錄（選用）**。

### env 與 DB 的關係
- **A. DB 完全取代 env**、**B. 兩者 OR、env 先查且短路（選用）**。

### 讀取失敗時的方向
- **A. fail-open**（讀不到 halt 狀態就放行）、**B. fail-closed（選用）**。

## Decision

- **D1**：可觀測性選 C —— `GET /admin/trading-status` + `POST /admin/dry-evaluate`（`25b7d5e`）。
  halt 判定**呼叫真的 `ManualKillGuard`** 取其 verdict；cap/buffer 報 value **加上哪一層 binding**，
  且 `resolve_for_symbol` 改為委派 `resolve_for_symbol_with_source`。
- **D2**：halt 選 C（DB `trading_halt`，`ddaf792`），並把 `canary.env` 的 flag 移除（`fe6de1d`）。
- **D3**：表形狀選 B（append-only；當前狀態 = 最大 `id` 那筆，**排序用 `id` 不用 `created_at_ms`**）。
- **D4**：env 與 DB 選 B（OR，env 先查短路，作為 break-glass）。
- **D5**：選 B（fail-closed）。

## Rationale

- **D1**：log 與 metrics 都要人**推論**「所以現在會不會下單」，事故當下要的是直接答案；且
  guard 邏輯改變時報告會自動跟上（它問的是 guard 本身），不可能描述 money path 不遵守的規則。
  **相較 A/B 的代價**：多兩個端點要維護與鑑權。委派 `_with_source` 而非另寫一份解析，是因為
  cap 事故的本質就是「報告與實際綁定不是同一次解析」。
- **D2**：env 的 halt **一個 `git revert` 就靜默恢復真錢放貸**，不需任何東西壞掉。檔案（B）好一點
  但仍綁在單機且無稽核。**代價**：submit path 上多一次 DB 查詢，且 DB 成為交易的新依賴（由 D5 承接）。
  移除 canary.env 的 flag 是必要的——**永遠先短路的開關會遮蔽持久路徑是否可用**，留著等於放棄驗證。
- **D3**：「誰在何時恢復真錢交易、理由為何」正是本次事故完全答不出來的問題，**覆寫式的單列答得更少**。
  **代價**：多一張會持續長大的表（每次 halt/resume 一列，量極小）。排序用 `id` 而非時間戳，是因為
  時鐘偏移或補寫的列不該讓已被取代的**安全**狀態復活。
- **D4**：DB 掛掉時仍必須能停機，所以保留 env 為 break-glass 且先查短路。**相較 A（DB 取代 env）
  的代價是「兩個真相來源」**——但兩者同方向（任一為真即 halt），不存在 cap 那種「哪個才算數」的
  歧義，且 `/admin/trading-status` 分開回報兩個來源，因為**哪個機制在擋決定了怎麼解除**。
- **D5**：**與同模組 `NavPeakStore` 的 fail-permissive 刻意相反**（丟失 nav_peak 只是回退高水位，
  放行合理）。**代價**：DB 不可用時全面停止交易——對放貸機器人這是正確的保守方向，且 chain 中
  `WriterLockGuard` 早已有相同依賴。code 內明寫兩者不可統一形狀。

## Expected Outcome

- 「現在會不會下單、被誰擋」隨時可答，不需等資金或流量湊巧出現 → 停機類事故的驗證不再退化成套套邏輯
- 任何一次 deploy / revert / branch 切換都不會意外恢復真錢交易
- 恢復交易留下 who/when/why 的稽核軌跡
- 判斷成功：下次停機時，能在 60 秒內以 `dry-evaluate` 輸出證明它擋得住（2026-07-27 首次達成：
  fUST 仍有 headroom、其餘 guard 全放行、只有 `manual_kill` 擋）

## Followup

- 恢復交易（gate：24h L3 驗收，07-28 02:13 UTC）——手段是 `POST /admin/resume?confirm=true`
- VM 上 `bfx-halt-watch` / `bfx-l3-verify-24h` 仍是 transient unit，重開機即消失；永久 unit
  已在 `deploy/vm/systemd/`，待安裝
- `/metrics` 尚未加 halted gauge；Prometheus 告警規則「halted=true 卻出現 `RESERVATION_INTENT`」未建
- P3–P5 未做：cap 收斂成單一來源（設了不生效的值應在啟動時 fatal）、deploy 關鍵邏輯移出 shell

## Invariants

- 報告中的 effective cap/buffer 必須與 guard 實際解析**同源**（`resolve_for_symbol_with_source`），
  不得另寫第二份解析
- `dry_evaluate` 不得 emit、不得記 heartbeat、不得可達 executor
- halt 狀態讀取失敗一律視為 halted

## Revocation Triggers

- 當 halt 的讀取延遲成為 submit path 瓶頸 → 重評（可加快取，但須有明確失效上限，且不得讓 unhalt 延遲生效）
- 當出現第二個需要持久化的 operational 開關 → 重評（抽成通用 flag 機制，而非再開一張表）
- 當 daemon 變成多實例 → 重評（env break-glass 只作用於單一進程，屆時語意會分裂）

## Lessons

### Rules

- **R1**：驗證一個設定「已生效」時讀回該設定本身（`printenv` / `cat config`），在「生效」與
  「完全無作用」兩種世界輸出相同。`Rule: 驗證設定生效必須驗行為不驗輸入——讓系統做一次它被設定要
  阻止的事；若因前置條件不足做不出來，那是「驗證不了」而非「驗證通過」，該補的是 dry-run 介面。`
- **R2**：同一個模組裡 `NavPeakStore`（fail-permissive）與 halt（fail-closed）的失效方向相反，
  若為了「一致性」統一成同一種形狀，其中一個必然是錯的。`Rule: 狀態儲存的失效方向由「丟失這份狀態
  的後果」決定，不由模組一致性決定；安全控制一律 fail-closed，並在 code 中明寫它與鄰居相反。`

### Observations

- **O1**：這兩個端點上線後**第一次 live 執行就抓到自己的缺陷**（預設只探測 `symbols[0]`＝dark 的
  fUSD，會把人指向資金而非 halt，`09dbf7e` 修）。缺陷只有在「能查行為」之後才可見。

## Related

- **來源（Provenance）**：本 ADR 即原始紀錄，與 Claude Code 討論當場拍板；觸發事件為同日
  `3ad142c` 的無效暫停
- 主要 commits：`25b7d5e`（P0+P1 端點）、`09dbf7e`（probe 掃全 symbol）、`ddaf792`（halt 持久化，
  migration `d437f9d4e3fe`）、`fe6de1d`（移除 canary.env flag）、`3897162` + `e90a03a`（ops 腳本）
- 相關 ADR：[candle 不可變 + bitemporal](2026-07-27-candle-immutability-bitemporal.md)——本決策的
  觸發事故發生在該 ADR 的 D3「暫停 canary」執行過程中，並證偽了其原始手段
