# 第一輪策略架構修正

## 目標

先修四個已重現的程式問題，並修正 A/B 判讀規則。完成點是本機實作、回歸測試與 review；提前還款及浮動 FRR 的完整收益模型另列後續研究。

## 實作

- **回測缺資料**：缺少指定市場序列的 timestamp、有效價格或期限證據時，回傳 `BacktestIncomplete`；移除保證成交與沿用觀察價格的隱性 fallback。資料完整的既有案例維持結果。
- **重掛一致性**：重用 `PeriodPricer`，以相同 symbol、期限及剩餘掛單金額取得當下定價，再套用原有 tolerance、最低掛齡與 cancel budget。同期限有多個有效意圖時取最高定價；book 或對應意圖不可用時不因 reprice 取消。保留既有 cancel guards 與下一輪 reconcile 才釋放資金的流程。
- **成交時間邊界**：依 candle 完整時間區間判斷 horizon，禁止把截止時間後的成交量算入。無法判定或觀察期間不足時標為未知，不混成成功／失敗樣本；不足以形成有效估計的 bucket 回報 unavailable。更新 model version，要求重建受影響 artifact。
- **optimizer**：signal、maker、taker 各自取得其價格的 fill evidence；評分與 audit 使用同一份候選證據，送單 gate 使用最終選中候選的證據。期限與 reference 定義必須符合模型；不相容時沿用既有 unavailable 處理。
- **A/B 判讀**：CI 跨零改為 `INCONCLUSIVE`，同步既有 ADR 與引用它的研究文件；本輪不自行設定 non-inferiority margin。

## 驗證

- 重現並修正：缺價仍 100% 成交、60 分鐘 horizon 回報 83 分鐘、市場不變仍取消重掛、共用 fill evidence 導致候選排名翻轉。
- 覆蓋不同期限、過期 book、缺失候選證據、觀察窗不足，以及 optimizer shadow 不改送單價格。
- 執行相關測試、後端全部 non-integration tests、`mypy src/`、`ruff check`；補跑受影響的 cancel／audit integration tests，完成 money-path review。

## 邊界與交付

保留使用者現有變更；維持目前策略參數、資金政策與 optimizer 啟用狀態。交付修正 diff、驗證結果及 artifact 重建清單。VM 重建模型、部署與真實 A/B 起跑屬下一個操作階段。

## 狀態（2026-09-23）

| 項 | 落點 | 狀態 |
|---|---|---|
| 回測缺資料 | `engine.py`：缺 mts／缺 close 一律 `BacktestIncomplete("market_series_gap")`，含 `market_candles` 路徑；`period_structure.align_series` 先對齊共同 slot 並在報告註明丟掉幾格；fixtures 報告已重跑（數字動 ≤0.0002%/mo） | ✅ |
| 成交時間邊界 | G13 與 book-replay 兩個 learner 都只計「完整落在 (決策時刻, 截止] 內」的 candle；不足以判定的 horizon 不成樣本；`g13-candle-v2`／`book-replay-v2` | ✅（artifact 重建清單：目前兩種 artifact 皆未在 VM 生成過，無舊物需作廢） |
| optimizer | signal 與 exact-period 候選各自估自己價格的 evidence；沒有自身 evidence 的候選不參與評分；OPTIMIZER_LIVE 的送單 gate 帶選中候選的 evidence | ✅ |
| A/B 判讀 | ADR 2026-09-22 A/B 加 Amendment：跨 0 = INCONCLUSIVE，無 margin；review §4 與 registry 同步 | ✅ |
| 重掛一致性 | `RepricePolicy.reference`＝`quote`（預設、現行）或 `book`（PeriodPricer 對當下 book、同期限、該 offer 剩餘金額重定價，多個 quote 取最高；book 或期限 quote 不可用不砍）；env `BFX_REPRICE_REFERENCE` | ✅ 程式碼；**live 尚未切換** |

第 5 列的 rollout 另走：加 `config_regime` 列 → `live.env` 設 `BFX_REPRICE_REFERENCE=book` → 先 `BFX_REPRICE_ENABLED=false` 觀察 `reprice_would_cancel` 的 ref_rate 一段時間 → 啟用 → 用 `report_execution_quality` 比 fill 率與 cancel 頻率。不與 book WS 修正同一個 release。
