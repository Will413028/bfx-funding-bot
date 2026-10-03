---
title: Go 時代 OpenSpec 設計的處置——Python 版刻意反轉的六條運作原則與四條未裁決契約
date: 2026-09-23
status: active
tags: [bfx-funding-bot, decision, architecture, spec-compression, fail-closed]
related-commits:
  - "eecce02^..2a76674"
  - d64fb26
---

# Go 時代 OpenSpec 設計的處置

## Context

2026-03-08～03-23 以 OpenSpec 為 Go 後端（Gin＋fx＋sqlc＋Atlas）寫了 82 個 change 和 78 份 capability spec，當時目標是多租戶 SaaS。2026-05-09 決定整個後端改寫成 Python，Go 在 05-29 刪除，但 `openspec/` 一直留著沒處理。2026-09-23 做壓縮時，527 個檔全文讀過。結果：13 個策略模組沒有一個進到現行 live 算法；運作模型的核心原則有六條被 Python 版刻意反轉，而且多數已有獨立 ADR；另有四條安全或 venue 契約在改寫時**沒有任何明文裁決**就消失了。

## Options Considered

- **A. 延續 Go 時代的設計**：多租戶 per-user worker pool、3 分鐘 tick、13 模組 composite 定價、graceful degradation（缺 book 退回 FRR-only、tick 內 recover、circuit breaker＋retry）、多個獨立部署器、15 分鐘 zombie TTL、config 熱載入。
- **B. 單帳戶、fail-closed、研究先行（Python 版實際採用）**：一個 `BFX_EXCHANGE_ACCOUNT_ID` 一個 daemon，1h 訊號＋90s reconcile；證據不足一律 block；資金只有一個寫入者；策略必須先過 EDA／回測才能上 live。
- **C. 保留 Go 的多租戶骨架，只替換策略層**：來源與 2026-05-09 的改寫決策都沒有認真評估這個選項。它保留了 A 的 per-user 隔離與 quota，但 A 的失敗語意（靜默降級）也會一起保留下來。

## Decision

以下 D 項對應原始 design 標題（路徑見 Related）：

- **D1 運作模型**：單帳戶 daemon＋1h 訊號＋90s reconcile，不採 per-user worker pool＋3 分鐘 tick（`worker-pool`、`lending-engine` §1/§2、`engine-orchestrator`）。
- **D2 失敗姿態**：fail-closed，不採 graceful degradation。來源的四個選擇全部反轉：`g9-graceful-degradation` §4（缺 book → FRR-only）、`worker-lifecycle` §4（tick 內 recover 後繼續）、`production-hardening` D1（circuit breaker＋retry）、`signal-modules` §4（資料不足時輸出中性值）。
- **D3 資金回收只有一個寫入者**：到期續約、利息再投資、batch expiry、分批部署、idle urgency 都不另設部署器，全部交給 gap 分配器處理（`credit-management` §3、`interest-reinvest` §2、`batch-expiry`、`m7-temporal-laddering`、`p1` D7）。
- **D4 過期掛單**：只往下 reprice（>quote 10%、掛齡 ≥30min、每 tick ≤3、沒有 quote 就不撤），不採固定 15 分鐘 zombie TTL（`lending-engine` §3、`p1` D8）。
- **D5 策略 config**：沿用 JSONB，但不熱載入；draft ≠ applied，要經 release session 才生效（`strategy-config` §1、`worker-lifecycle` §3、`engine-integration` §2/§6）。
- **D6 延續的選擇**：Bitfinex client 自寫、不用 SDK（`bitfinex-client` §1）；憑證用 AES-256-GCM，演進為 DEK/KEK envelope＋account-UUID AAD（`apikey-management` §1）；驗證失敗仍允許儲存為 unverified（`apikey-verification` §1）；歷史資料兩表分開＋自然複合 PK＋one-shot runner＋循序抓取（backfill D1/D2/D4/D7）。反轉的是 nonce：同一把 key 共用單一 µs 單調來源，取代每個 client 各自的 ms＋counter（`bitfinex-client` §2）。

## Rationale

- **D1**：B 的代價是放棄多租戶，SaaS 化時要重建隔離層（已在 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) 以 account-isolated worker 規劃）。不選 A，是因為小額單一帳戶用不到 quota／pool；而且 A 的 3 分鐘 tick 比 venue 限流（public 30 req/min）與 candle 定稿節奏都快，只是在空轉。
- **D2**：這是真錢路徑，靜默降級等於「用比較差的證據照樣下單」，D2 列出的四個來源選擇都屬於這一類。代價是可用率：book 不新鮮時就停止送單，所以 book 新鮮度本身變成要維運的指標（[2026-08-23-funding-strategy-execution-integrity](2026-08-23-funding-strategy-execution-integrity.md)）。
- **D3**：多個部署器動用同一份 available 餘額會重複部署，來源的 Risks 段自己就寫了 "Double deployment"。單一寫入者（[2026-05-29-deployment-reconciler](2026-05-29-deployment-reconciler.md)）犧牲了「續約沿用原 rate」這類局部最佳化。
- **D4**：固定 TTL 會把正常排隊中的掛單撤掉，放棄隊列位置。代價是掛單可能停在高於市場的價位更久，由 E1 的 10% 容差設上限（[2026-07-06-e2-book-aware-clamp](2026-07-06-e2-book-aware-clamp.md)）。
- **D5**：熱載入讓「目前 live 在跑哪一套參數」無法稽核；改成 release session 之後，每次套用都有 revision 和 canary。代價是改參數要走一次 release 儀式。
- **D6**：自寫 client 的理由在兩種語言都成立（需要的 endpoint 少、SDK 沒有增加抽象），Python 版重新評估過官方 SDK 後仍然不選。nonce 的反轉來自實際事故：auth WS 從 4.4a 起就沒認證成功過。

## Result

- `git log --oneline eecce02^..2a76674` 是 Go 時代的實作區間；`d64fb26` 是最後一個碰 openspec 的 commit（backfill change，同日被改寫決策取代）。
- 策略模組的否定結論各自落在既有 ADR：weekend 對應 [2026-05-18-phase3b-walk-forward-over-single-split](2026-05-18-phase3b-walk-forward-over-single-split.md)；FRR 作為 base／floor 對應 [2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)；訊號模組對應 [2026-06-06-signal-eda-funnel](2026-06-06-signal-eda-funnel.md)；切單對應 [2026-07-10-strategy-review-e2-enforce-and-chunking-verdict](2026-07-10-strategy-review-e2-enforce-and-chunking-verdict.md)；「已收盤 candle 不會變」對應 [2026-07-27-candle-immutability-bitemporal](2026-07-27-candle-immutability-bitemporal.md)。本 ADR 不重述這些結論。
- 驗證：四個 agent 分區全文讀取，檔案數與 bytes 加總（527 檔、967,531 bytes）與 `find | wc -c` 一致；D1–D6 的現況以 `rg backend_py/src` 逐項核對。

## Followup

以下四條契約在改寫時消失，找不到任何明文裁決。2026-09-24 已全部裁決為放棄，理由見 Updates：

- ✅ **放棄 WS `dms:4`**：斷線時由交易所自動撤單（`websocket-client` §5）。現行程式碼沒有 dms，走的是「orphan 只 quarantine」路線。兩種做法的取捨是：斷線期間的 resting offer 要不要被撤。
- ✅ **放棄 auto-renew 安全網**：spec 要求 credit 一律開 auto-renew，daemon 掛掉時避免資金閒置（`lending-strategy`）。現行送單 `flags: 0`；halt 或 daemon 掛掉期間，到期的資金會閒置。
- ✅ **放棄利率崩跌暫停**：FRR 日跌 ≥30% 時凍結 5 分鐘（`flash-crash-detection`）。現行 L2 guard 看的是 NAV，沒有「市場崩跌時不下新單」這一層。
- ✅ **放棄 config 參數上下限**：rate ≤0.01、period 2–120、amount ≥ venue 下限（`strategy-config-validation`）。現行只檢查「正數且 min≤max」。
- ✅ **已完成（`0f80647`）**：Bitfinex 429 自動退避沒做（backfill D6）。`BitfinexRateLimited(retry_after)` 只有 raise，沒有任何呼叫端依 retry_after 退避。研究抓資料和 live 共用同一個 IP 的限流額度。
- ✅ **已完成（`b968a4c`）**：WS info code 20051（重連）與 20060／20061（維護）沒有處理，現行只靠一般斷線 backoff。

## Updates (2026-09-24)

四條消失的契約全部明文放棄（Will 裁決），429 退避已補上：

- **dms:4**：官方文件只寫 socket 關閉時「cancel all account orders」，沒說會取消 funding offer。就算會，斷線撤單也只會讓資金閒置；放貸掛單在斷線期間成交最多是利率不夠好，不會虧本金，而 REST reconcile 每 90 秒會對帳。
- **auto-renew 安全網**：halt 的語意是「不再產生新曝險」。開了 auto-renew，交易所會在 halt 期間繼續替 bot 放貸，也會污染 A/B 實驗裡以 auto-renew 當基準的 B 臂。所以接受停機期間資金閒置。
- **利率崩跌暫停**：E2 book clamp 已經限制報價最多往下調 15%，期限固定 2 天；崩跌時放貸只是收益變低，不會虧本金；FRR 也已判定不是市場利率（[2026-05-28-frr-not-a-market-rate-proxy](2026-05-28-frr-not-a-market-rate-proxy.md)）。
- **config 參數上下限**：config draft 是前端用的草稿，daemon 開機時載入後從不讀取；live 送單的參數來自 `cells.live.yaml` 和 CapitalPolicy，送單前還有 `validate_amount` 依交易所規則檢查。加上下限只能擋掉無意義的草稿，碰不到真錢路徑。
- **429 退避**：`0f80647`。只有批次腳本開啟（60→120→240→240 秒，取 Retry-After 與排定秒數較大者），live daemon 維持不重試。
- **WS info code**：`b968a4c`。維護期間（20060 或連線時 `platform.status = 0`）book 以 `book_venue_maintenance` fail-closed，REST 補救也拒絕，維護期間看到的資料在結束時一律作廢；20051／20061 讓三個 WS client 主動重連，auth 重連會觸發帳本重新同步。Followup 全數收口。

## Revocation Triggers

- 開始 SaaS 多租戶實作 → 重評 D1，以 account-isolated worker 取代「單帳戶」前提。
- 任何一條 Followup 契約裁決成「補回」 → 在本 ADR 補 `## Updates` 一行，指向新 ADR。
- config draft 進入 live 送單路徑 → 在那個改動裡一併設計參數上下限（rate、period、依匯率換算的金額下限）。
- 確認 `dms:4` 會取消 funding offer，或 bot 開始下 trading order → 重評 dms。

## Lessons

### Rules

- **R1**：改寫時舊 spec 的 safety／venue 契約（dms、auto-renew、flash freeze、參數上下限）沒有逐條對照，四條無聲消失，四個月後壓縮舊 spec 才發現。Rule: 大改寫前把舊系統的安全與 venue 契約列成清單，逐條標記 keep／drop＋理由；drop 也是一個需要寫下來的決定。

### Observations

- **O1**：13 個策略模組的常數全是沒回測過的 expert prior，各模組單測全綠，但從未組合回測過。後來 EDA 否定了 7/8 的訊號，印證「單元測試綠 ≠ 策略有效」。

## Related

- 來源（Provenance）：openspec 全部 527 檔（例如 `openspec/changes/archive/2026-03-08-websocket-client/design.md`）；原文已不在 repo，本 ADR 即紀錄。
- 改寫決策來源：`2026-05-09-backend-rewrite-to-python-decision.md`（原文已不在 repo，本 ADR 即紀錄）。語言、遷移路徑、stack 的取捨已壓縮成 [2026-05-09-python-rewrite-and-day3-stack](2026-05-09-python-rewrite-and-day3-stack.md)；本 ADR 只處理 Go 時代語意的去留。
- 流程退場：2026-05-09 起 OpenSpec 流程由 superpowers 流程取代。
- Go 時代策略 review 紀錄 `docs/strategy-journal.md`（2026-03-21 三輪 review：P0 pipeline 未接線、GT1–GT5 調參、S1–S10／M1–M8 提案）已由本 ADR 逐項處置（原文已不在 repo，本 ADR 即紀錄）。
- 相關 ADR：[2026-05-27-reconcile-as-correctness-backbone](2026-05-27-reconcile-as-correctness-backbone.md)、[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)（auth／BFF／限流由 Better Auth 接手）、[2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md)、[2026-07-27-halt-control-and-behaviour-observability](2026-07-27-halt-control-and-behaviour-observability.md)、[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)。
