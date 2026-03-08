## Context

引擎目前執行放貸操作後無持久化。需要 `executions` table 紀錄每筆操作，並提供 REST API 供用戶查詢歷史。

## Goals / Non-Goals

**Goals:**
- 定義 `ExecutionRecord` domain type，涵蓋 place/cancel/filled/renew 四種操作類型
- 新增 `executions` table，支援按 user + 時間範圍查詢
- 提供 `GET /api/v1/executions?since=&limit=` API
- 全層實作：domain → schema → sqlc → repo → service → handler → router

**Non-Goals:**
- Alpha report / 績效分析（留給未來功能）
- 自動觸發紀錄（由 lending engine 呼叫，本次只建 infrastructure）

## Decisions

### ExecutionRecord 欄位

| 欄位 | 型別 | 說明 |
|------|------|------|
| ID | uuid | PK |
| UserID | uuid | FK → users |
| Action | string | place / cancel / filled / renew |
| Currency | string | e.g. "fUSD" |
| Amount | float64 | 金額 |
| Rate | float64 | 日利率 |
| Period | int | 天數 |
| OfferID | int64 | Bitfinex offer ID (nullable) |
| Status | string | success / failed |
| ErrorMessage | string | 失敗原因 (nullable) |
| CreatedAt | time.Time | 建立時間 |

### 查詢 API 設計

`GET /api/v1/executions?since=2026-01-01T00:00:00Z&limit=50`

- `since` — RFC3339 時間戳（預設 7 天前）
- `limit` — 最大筆數（預設 50，上限 200）
- 回傳按 `created_at DESC` 排序

### 分層架構

遵循既有三層模式：handler → service → repository，fx 注入。

## Alternatives Considered

### 用 JSONB 存操作明細
結論：reject。結構化欄位利於索引查詢和未來帳單聚合。
