## Why

Frontend scaffold (F1) 已完成，但尚缺核心工具層：API 請求封裝、型別定義、環境變數驗證、格式化函式。後續所有 Dashboard 頁面（F5-F10）及 Auth 頁面（F4）都依賴這些基礎模組，必須先建立。

## What Changes

- 新增 `src/lib/api-client.ts` — 封裝原生 fetch，Client Component 走 `/api/proxy` 同源代理，Server Component 直連後端；提供 `get<T>()` 自動解包 data + `getList<T>()` 保留分頁結構；統一 `ApiError` 錯誤類別
- 新增 `src/lib/format.ts` — 放貸平台專用格式化：年化利率 (APR)、日利率、USD 金額、放貸天數
- 新增 `src/lib/env.ts` — Zod schema 驗證環境變數，區分 client/server 變數，啟動時 fail fast
- 新增 `src/types/index.ts` — 對應 Go backend `domain/` 的完整 TypeScript 型別（User、ApiKey、UserConfig、Dashboard、Earnings、Billing、Execution 等）
- 新增 `src/lib/query-keys.ts` — TanStack Query Key Factory，集中管理所有 query key

## Capabilities

### New Capabilities
- `api-client`: fetch 封裝（同源代理 vs 直連）、data 解包策略、ApiError 錯誤處理
- `frontend-types`: 前端型別定義，對應 Go backend domain 型別與 API 回應格式

### Modified Capabilities

（無既有 capability 需修改）

## Impact

- `frontend/src/lib/` — 新增 3 個檔案（api-client.ts, format.ts, env.ts）+ 1 個既有檔案不變（utils.ts）
- `frontend/src/types/` — 新增目錄與 index.ts
- `frontend/src/lib/query-keys.ts` — 新增 Query Key Factory
- 後續 F3 (API Proxy) 及 F4-F10 頁面開發均依賴此 change
