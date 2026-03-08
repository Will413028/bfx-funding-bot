## Context

F1 scaffold 已建立 Next.js 16 + Tailwind v4 專案骨架。目前 `src/lib/` 僅有 `utils.ts`（cn 函式），缺少 API 請求封裝、型別定義、環境變數驗證等核心工具。後續所有頁面開發（F3-F10）均需這些模組。

Go 後端已完成所有 API endpoints，JSON 回應格式統一：成功 `{ "data": T }`、錯誤 `{ "error": { "code": "...", "message": "..." } }`、分頁 `{ "data": T[], "pagination": { "nextCursor": "...", "hasMore": true } }`。

## Goals / Non-Goals

**Goals:**
- 建立可被所有頁面共用的 API 請求層（支援 SSR 直連 + CSR 同源代理）
- 定義完整前端型別，與 Go backend domain 一對一對應
- 環境變數啟動時驗證，缺少即 fail fast
- 提供放貸平台專用的格式化函式
- 集中管理 TanStack Query keys

**Non-Goals:**
- 不實作 API proxy route（F3 範疇）
- 不實作 WebSocket client（F12 範疇）
- 不實作 Zod form validation schemas（F4 範疇）
- 不實作 Zustand store（F12 範疇）

## Decisions

### D1: api-client 雙環境自動切換

Client Component 走 `/api/proxy` 前綴（同源代理），Server Component 走 `API_URL` 直連。用 `typeof window === "undefined"` 判斷。

**替代方案**：分成兩個 client（`serverApiClient` / `clientApiClient`）→ 呼叫端需知道自己在哪個環境，增加認知負擔。

### D2: get() vs getList() 解包策略

- `get<T>(path)` — 自動解包 `{ "data": T }` → 回傳 `T`
- `getList<T>(path)` — 不解包，回傳完整結構（含 pagination）

**理由**：單一 `get()` 方法無法同時兼顧「自動解包方便」和「保留分頁結構」。兩個方法語意明確，呼叫端一看就知道拿到什麼。

### D3: ApiError 類別化

繼承 `Error`，帶 `status`、`code`、`message` 屬性，方便 TanStack Query 的 `onError` 和 error boundary 處理。

### D4: env.ts 分層驗證

`clientEnvSchema`（NEXT_PUBLIC_* 變數）和 `serverEnvSchema`（extends client + server-only 變數）。runtime 判斷環境，各自驗證。

**替代方案**：`@t3-oss/env-nextjs` → 多一個依賴，對我們的簡單需求來說過度。

### D5: query-keys factory pattern

每個 resource 一個 object，方法回傳 tuple（`as const`）。與 TanStack Query v5 的 query key factory 慣例一致，支援精確 invalidation。

## Risks / Trade-offs

- [env.ts 在 build 時執行] → Client 變數缺少會在 build 階段報錯（Vercel deploy fail），這是刻意的 fail fast 行為
- [型別與後端不同步] → 目前手動維護，未來可考慮 OpenAPI codegen，但現階段後端型別穩定且數量不多
