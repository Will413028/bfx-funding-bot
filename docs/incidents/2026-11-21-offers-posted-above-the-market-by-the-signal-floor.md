# 2026-11-21 恢復放貸後三張單掛在市場的 2.2 倍

- 狀態：已恢復（修補待發版）
- 嚴重度：交易停擺（bot 有掛單但掛在成交不了的價位）
- 期間：開始 2026-11-21 16:51 UTC（第一張單送出，bot log `order_submit`）→ 偵測 16:57 UTC（人工比對掛單利率與公開 book）→ 修補合併與發版時間見修補 PR

## 影響

- fUST，三張各約 200 UST 的 2 天期掛單（offer id `5186942142`、`5186942924`、`5186943861`），掛在 0.00029645／日，到 2026-11-21 17:24 UTC 都沒有成交。同一時間公開 book 的 2 天期最低賣價是 0.0001334、最高買價 0.0001301。
- 沒有成交，所以沒有以錯誤利率借出的金額；帳本、餘額、對帳沒有寫錯。影響是這 600 UST 在這段時間等同閒置。
- 其餘約 353 UST 沒有掛出，與本事故無關（另一個 cell 當小時的訊號是不放）。

## 時間線

| 時間（UTC） | 事件 | 來源 |
|---|---|---|
| 15:13 | operator 恢復放貸，`HALTED` → `ACTIVE` | `bfx-op resume` 輸出、bot log |
| 16:00 | 訊號層：`fUST_a30` 判定 post，訊號利率 0.00029645；`fUST_p2` 判定 skip | bot log `signal` |
| 16:51–16:54 | Funding 錢包出現餘額後，連續三個部署週期各送出一張單，定價分支都是 `signal_floor` | bot log `order_submit`、execution decision |
| 16:57 | 人工比對：掛單利率是 book 最低賣價的 2.2 倍 | 公開 book `GET /v2/book/fUST/P0` |
| 17:00 | 訊號層兩個 cell 都判定 skip；沒有 active quote，舊單依設計不撤 | bot log `signal` |

## 原因

- 觸發點：整點訊號利率（a30 聚合序列的收盤價）遠高於當下 2 天期 book。
- 根因：`PeriodPricer` 在「最低賣價減一檔」低於訊號利率的 85% 時，改掛訊號利率（`signal_floor` 分支，`BFX_BOOK_MAX_DOWN_PCT=0.15`）。這條下限的用意是不以舊收盤價賤賣，但它把單留在 book 上方，而訊號利率一小時才更新一次。位置：修補前的 `backend/src/bfx_funding_bot/modules/execution/deployment/period_pricing.py`。

## 偵測與處理

- 沒有告警會報這種情況：單有送出、venue 有 ack，各項健康檢查都正常。是在確認恢復放貸是否生效時，人工把掛單利率和公開 book 比對才發現，距第一張單約 6 分鐘。
- 處理：不手動改單（放貸全自動的原則）；改定價規則（ADR `docs/adr/2026-11-22-offer-price-follows-the-book-below-the-signal.md`）。

## 回歸測試

- 測試：`backend/tests/modules/execution/deployment/test_period_pricing.py::test_offer_follows_a_book_at_less_than_half_the_hourly_signal`
- 修補前失敗的證據：在修補前的程式上（`origin/dev` 的 `840492af` 加上這條測試）定價回傳訊號利率而不是 book 價，同批另一條 `test_exact_period_ask_far_below_signal_is_still_followed` 斷言失敗（`2 failed, 8 passed`）；修補後 `tests/modules/execution/deployment` 全過。
- 修補 PR：#457

## 後續

| 項目 | 類型（預防／偵測／緩解） | 追蹤（PR、issue） |
|---|---|---|
| 定價不再以訊號利率當下限；safety guard 改檢查實際要送出的利率 | 預防 | PR #457 |
| 沒有「掛單利率遠離 book」的告警：單掛在成交不了的價位時系統自己不會發現 | 偵測 | 未排定，記在 owner 的待辦 |
| 沒有 active quote 時舊單不撤（設計如此）：訊號轉為 skip 後，高掛的舊單會一直留著 | 緩解 | 未排定，隨「放／不放開關」的去留一起決定 |
