# 需求清單與現況盤點（快照 `80fe72c2`，2026-11-22）

來源：owner 2026-11-22 核可的實驗設計（下稱「設計」）；ADR `docs/adr/2026-09-27-keep-always-2d-over-rate-threshold-tenor.md`（D2、Followup、Revocation Triggers）。預登記原文在 repo 外的研究 registry（見本機個人脈絡檔）。

## 需求

| # | 需求 | 來源 | 對應步驟 |
|---|---|---|---|
| R1 | 只在 fUST 測 7 天期 | 設計 | 2 |
| R2 | 掛 7 天的條件：book 上有夠深的 7 天期賣單，且其利率 ≥ max(0.0002／日, 當下 2 天期利率)；否則照常掛 2 天 | ADR 09-27 D2 的選項 D | 2 |
| R3 | 同時最多 3 筆 7 天期借款，合計 ≤ 600 USDt；單筆沿用包絡的 `max_offer_amount` | 設計 | 1、2（「筆」的數法見計畫 D5） |
| R4 | 第零步：先只放 1 筆，成交後看借款記的天期；記 7 天才繼續 | 設計 | 4、5、6 |
| R5 | 停止：累計 20 筆已結束的 7 天期借款，或開跑後 10 週，先到先停；實驗程式引發自動停機也停 | 設計 | 2、8（樣本上限與截止日由規則自己執行）；自動停機的部分不自動判定，由 owner 看告警後決定（計畫前提段、步驟 3 的 runbook、步驟 9） |
| R6 | 全程由 bot 掛單，不靠手動掛單 | `AGENTS.md`「放貸全自動」 | 2、3 |
| R7 | 量四個指標（位置、持有時間中位數佔期限、2 天期利率跌破鎖定利率 20% 後 24 小時內結束的比例、7 天期與同期 2 天期每 USDt 每日實收利息差與 bootstrap 95% CI），只看 credit 列 | 設計 | 7 |
| R8 | 沒成交的 7 天期單不能一直掛著 | 推論（現有撤單不處理，見盤點 4） | 3 |

沒有來源、由計畫補上的參數（都不改變預登記的內容）列在計畫的「前提」段，標「預設（可推翻）」。

## 非功能需求

- 預設關閉：沒有設定新環境變數、或包絡的 `max_period_days` 仍是 2 時，行為與現在逐欄相同。
- 實驗資金上限由 R3 保證；不新增依賴；不新增 suppression。
- 可觀測：每次選 7 天或退回 2 天都寫進 execution decision 的 evidence，能事後數出各走了幾次、為什麼退回。

## 現況盤點（新規則會接上的地方）

| # | 現況 | 位置 |
|---|---|---|
| 1 | 新單的天期來自每小時訊號的 `StandingQuote.period_days`，在部署迴圈裡抄進 `DecisionPayload.offer_duration_days`，之後定價、safety guard、execution gate、command gate、送單都只讀這個欄位 | `backend/src/bfx_funding_bot/modules/execution/deployment/reconciler.py:464,485-492`；`period_pricing.py:48,66-69`；`safety/pre_trade.py:144-148`；`deployment/eligibility.py:505,522`；`command_gate.py:217,484,522` |
| 2 | 部署層每個 tick 拿得到受管的掛單（含天期與建立時間） | `reconciler.py:348` 的 `venue_offers`；`deployment_input.py:45-54`；`external/bitfinex/auth_rest.py:89-104` |
| 3 | 部署層拿不到逐筆借款；借出的錢只出現在 `CapitalSnapshot.total_capital`。逐筆借款與天期在帳本的 `venue_credit_mirror`（`period_days`、`present_in_latest_accepted_snapshot`） | `modules/ledger/__init__.py:912-925`；`modules/trading/policy.py:111-122`；`modules/ledger/tables.py:535-565` |
| 4 | 撤單重掛（E1）在 `book` 參考模式下，只對「有同天期 active quote」的掛單定參考價；訊號都是 2 天期，所以 7 天期掛單永遠不會被它撤 | `reconciler.py:971-998`（`_book_reprice_reference_for`）；`deploy/vm/live.env` 的 `BFX_REPRICE_REFERENCE=book` |
| 5 | 包絡在送單前檢查天期範圍，超出時是一般的 skip（guard 名 `offer_envelope`）；`max_open_offers` 不分天期一起數 | `safety/pre_trade.py:113-114,141-149`；`reconciler.py:604-619` |
| 6 | 改包絡用 `scripts/amend_capital_policy.py`（`--max-period-days`；先 dry run 拿 digest，再 `--apply-digest` 與 `--reason`），在 VM 以一次性容器、owner role 執行，不用發版 | `backend/scripts/amend_capital_policy.py:83-110`；`docs/runbooks/operations.md:61-79` |
| 7 | 部署層的可選政策物件由環境變數建立再傳進 reconciler（reprice、ladder 兩個先例） | `deployment/reprice.py:43-50`；`deployment/ladder.py:25-34`；`apps/bot.py:714-715,776-777`；`reconciler.py:186-187` |
| 8 | 一份 book snapshot 混著所有天期，每側 25 檔；7 天期價位不一定在裡面 | `modules/market/contracts.py:74-92`；`external/bitfinex/funding_book_ws.py:98`；`modules/market/book_period_coverage.py:9-15` |
| 9 | 已延後的項目「以 credit 自己的 `period` 計的天期上限」，觸發條件是「出現天期不同的 cell」 | `docs/plans/2026-11-14-cell-limit-resting-offers.md:65`；ADR `2026-11-14-cell-limit-bounds-resting-offers-not-lent-stock.md:81` |
| 10 | 持有時間報告只依 kind 分組，不分天期；loan 轉 credit 的判定比的是 credit 開始對 loan 結束，2026-11-21 實跑回 0 但逐筆看幾乎都是轉換 | `backend/scripts/report_credit_hold_times.py`；`modules/backtest/hold_times.py` 的 `loans_that_became_credits` |

## 外部介面

- Bitfinex：掛單天期 ≥ 借方要的天期即可成交（[Bitfinex support](https://support.bitfinex.com/hc/en-us/articles/115003428165-Margin-Funding-on-Bitfinex)，2026-11-20 讀）。成交後借款記的是哪一個天期：**未知**，沒有自己的樣本（所有掛單都是 2 天期），是 R4 第零步要回答的事。
- 7 天期掛單在 top-25 book 的可見率 42–61%（fUST，2026-07-19..09-27，ADR 09-27 Result）；條件 R2 在同一段期間約 16% 的小時成立（同一份研究報告）。重查方法：`uv run python -m scripts.audit_book_period_coverage --symbols fUST --periods 2,7 --since-ms <起點>`。

## 現況盤點補充（第 1 輪計畫審查後）

| # | 現況 | 位置 |
|---|---|---|
| 11 | 定價器在 book 上有借方出價 ≥ 訊號利率且量夠時，直接掛訊號利率（taker 分支），不看賣價。訊號利率來自 2 天期的每小時訊號，可以低於 0.0002／日 | `backend/src/bfx_funding_bot/modules/execution/deployment/period_pricing.py:71-87`；`reconciler.py:485-490` |
| 12 | 部署迴圈在同一個 tick 內對每個 cell 各送一筆；正式環境有兩個 fUST cell | `reconciler.py:449`；`backend/configs/cells.live.yaml` |
| 13 | 帳本記得自己送出的掛單的天期、原始與剩餘金額、結束方式；受管掛單帶 `signal_correlation_id` | `modules/ledger/tables.py:485-515`；`modules/ledger/__init__.py:751-768` |
| 14 | 撤單額度來自 `RepricePolicy`；E1 沒有 active quote 時整段不跑；撤單介面必須帶 `signal_correlation_id` | `reconciler.py:356-358,437,898-901`；`modules/execution/protocols.py:140-145` |
| 15 | 每週歸屬報告與 G3 讀借款的 `period_days` 並依 cell 彙總 | `modules/live_validation/credit_attribution.py:176-179`；`attribution_loader.py:181-285`；`credit_history.py:126-135` |
