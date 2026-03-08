## Context

Dashboard API 已提供即時 funding 狀態。使用者還需要收益統計來評估策略效果。Bitfinex Ledger API (`POST /v2/auth/r/ledgers/hist`) 可取得帳戶歷史記錄，篩選 `description` 包含 "Margin Funding Payment" 的條目即可取得利息收入。

## Goals / Non-Goals

**Goals:**
- 從 active credits 即時計算預估每日收益和加權平均 APY
- 從 Bitfinex Ledger API 取得歷史利息收入（過去 7 天、30 天）
- 提供單一 `GET /earnings` endpoint

**Non-Goals:**
- 不做本地持久化利息記錄（直接查 Bitfinex API）
- 不做複利計算或回測
- 不做多幣種聚合（MVP 只看使用者設定的 currency）
- 不做圖表用的時間序列資料（只算聚合值）

## Decisions

### 1. 即時預估 vs 歷史累計

**選擇：** 兩者都提供

- **即時預估**：從 `GetActiveFundingCredits` 計算 `sum(credit.Amount * credit.Rate)` = 日收益
- **歷史累計**：從 Ledger API 篩選 "Margin Funding Payment" 條目，按時間範圍累加

**理由：** 即時預估讓使用者知道「現在每天賺多少」，歷史累計讓使用者知道「實際賺了多少」。

### 2. Ledger API 呼叫方式

**選擇：** `POST /v2/auth/r/ledgers/hist` with `category: 28`（Margin Funding Payment category）

Bitfinex Ledger API 支持 `start`/`end` 毫秒時間戳篩選，`limit` 預設 25，最大 2500。使用 `category: 28` 直接過濾 funding payment，不需要客戶端篩選 description。

**Response format:**
```
[ID, CURRENCY, null, MTS, null, AMOUNT, BALANCE, null, DESCRIPTION]
```

### 3. APY 計算

**選擇：** 加權平均日利率 × 365

```
weighted_daily_rate = sum(credit.Amount * credit.Rate) / sum(credit.Amount)
apy = weighted_daily_rate * 365
```

**理由：** 簡單直覺，與 Bitfinex rate 定義一致（rate = daily rate as decimal fraction）。

### 4. 平行呼叫

**選擇：** credits 和 ledger 兩個 API 平行呼叫

**理由：** 與 Dashboard 相同的模式，減少延遲。

## Risks / Trade-offs

- **[Rate Limit]** Ledger API 有 rate limit → 前端應設定合理 polling 間隔
- **[大量歷史資料]** 頻繁放貸的使用者可能有大量 ledger 條目 → 使用 `limit: 2500` 上限，超出的歷史不列入計算
- **[Category 28]** Bitfinex category 28 對應 Margin Funding Payment → 若 Bitfinex 更改 category 定義需要調整
