---
description: Bitfinex venue behavior that reconciliation and execution code relies on
paths:
  - "backend/src/bfx_funding_bot/external/bitfinex/**"
  - "backend/src/bfx_funding_bot/modules/execution/**"
  - "backend/src/bfx_funding_bot/modules/backtest/**"
---

# Bitfinex venue

- Bitfinex `/hist` 以 `MTS_UPDATE` 過濾：查特定 offer 的終態用 id 直查，時間窗只用來發現新物件。
- 本機與 venue 時間戳比大小一律用 `core/venue_time.py` 的 `VENUE_CLOCK_TOLERANCE_MS`／`HISTORY_QUERY_MARGIN_MS`，不自訂邊界。
- Bitfinex funding offer 完全不帶 client cid：對帳與 boot recovery 只用 `venue_offer_id`，不要假設有 cid 欄位。
- funding offer submit 沒有 venue 端去重：不得 retry（`retry.py` 只給 cancel）；改 executor 錯誤處理前，沿實際 raise／catch 路徑驗證。
- Bitfinex 已借出資金分在 `/funding/credits` 與 `/funding/loans`，算曝險要兩邊都讀；rate 在 [11]、period 在 [12]，改 parser 前先用真實回應逐欄核對。
- 公開 funding 成交紀錄 `/v2/trades/f*/hist` 的 AMOUNT 正負號是主動成交方：負＝借方吃放貸掛單，正＝放貸方吃借方掛單；私有 `auth/r/funding/trades` 的 AMOUNT 恆正、MAKER 恆為 null，判斷自己是不是 maker 要用 offer 掛出到成交的時間差。
- 用 Python 打 `api-pub.bitfinex.com` 要帶通用 User-Agent（預設 `Python-urllib` 會被 Cloudflare 回 403），分頁抓歷史要節流並處理 429。
