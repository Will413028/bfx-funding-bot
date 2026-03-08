## Context

SaaS 平台採月費制收費，分等級（free / starter / pro / enterprise），不同等級月費不同，可使用的功能也不同。需要訂閱方案 + 帳單紀錄系統。

## Goals / Non-Goals

**Goals:**
- 定義 `SubscriptionPlan` + `BillingRecord` domain types
- `users` table 新增 `plan` 欄位紀錄當前方案
- 新增 `billing_records` table 紀錄每月帳單
- 提供 `GET /api/v1/billing` 查詢帳單歷史 + 摘要
- 提供 `GET /api/v1/billing/plan` 查詢當前方案及權限

**Non-Goals:**
- 付款整合（Stripe 等付款處理留待未來）
- 方案升降級流程（暫時由 admin 手動設定）
- 自動扣款

## Decisions

### 方案等級

| Plan | 月費 | 功能 |
|------|------|------|
| free | $0 | 基本儀表板、手動放貸、1 組 API Key |
| starter | $9.99 | 自動放貸（固定利率策略）、email 通知 |
| pro | $29.99 | 進階策略（市場分析、多策略）、優先 API 配額 |
| enterprise | $99.99 | 所有功能、專屬支援、自訂策略參數 |

### BillingRecord 欄位

| 欄位 | 型別 | 說明 |
|------|------|------|
| ID | uuid | PK |
| UserID | uuid | FK → users |
| PeriodStart | time.Time | 計費週期開始 |
| PeriodEnd | time.Time | 計費週期結束 |
| Plan | string | 該期方案 |
| Amount | float64 | 月費金額 |
| Currency | string | e.g. "USD" |
| Status | string | pending / paid / overdue / waived |
| PaidAt | *time.Time | 付款時間 |
| CreatedAt | time.Time | 建立時間 |

### 查詢 API 設計

1. `GET /api/v1/billing` — 帳單歷史 + 摘要
2. `GET /api/v1/billing/plan` — 當前方案 + 功能權限

### users table 變更

新增 `plan` 欄位（default: "free"），紀錄用戶當前訂閱方案。

### 分層架構

遵循既有三層：handler → service → repository，fx 注入。
