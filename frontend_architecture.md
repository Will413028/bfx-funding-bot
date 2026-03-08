# Bitfinex 自動放貸 SaaS 平台：前端架構設計文件

> 本文件為 Claude Code 開發前端時的架構參考。
> 基於 Next.js 16 App Router + next-intl 多語系架構，搭配 Go 後端 REST + WebSocket API。

---

## 技術棧總覽

| 領域 | 技術選擇 | 用途 |
|------|----------|------|
| 框架核心 | Next.js 16 (App Router) | SSR/SSG/ISR，路由，中介層 |
| UI 與樣式 | Tailwind CSS v4 + shadcn/ui | 原子化 CSS + 可客製化元件庫 |
| 元件工具 | class-variance-authority + tailwind-merge + clsx | 元件 variant 管理 + class 合併（`cn()` 函式） |
| 圖標 | Lucide React | 開源 SVG icon 庫，搭配 shadcn/ui 使用 |
| 動畫基礎 | tailwindcss-animate | shadcn/ui 動畫依賴（Tailwind v4 用 `@plugin` 載入） |
| 全域狀態 | Zustand | 客戶端狀態（UI 開關、主題、WebSocket 連線狀態） |
| 伺服器資料 | TanStack Query v5 | API 快取、重新抓取、樂觀更新 |
| 表單驗證 | React Hook Form + Zod v4 | 高效能表單 + 端到端型別安全驗證 |
| 語系切換 | next-intl | App Router 原生支援，Server/Client Component 皆可用 |
| URL 狀態 | nuqs | 帳單查詢、執行記錄過濾同步至 URL |
| 即時通訊 | 原生 WebSocket | Dashboard 即時推送（放貸狀態、市場數據） |
| 圖表 | Recharts | 利率走勢、收益圖表 |
| 程式碼品質 | Biome | 格式化 + Lint，唯一 linter（無 ESLint） |
| 未使用程式碼檢測 | Knip | 找出未使用的檔案、export、依賴、型別 |
| 部署 | Vercel | Next.js 官方平台，圖片自動優化、Edge Functions、ISR |

### 漸進式採用策略

- **Phase 1（必選）**：目錄結構、Tailwind + shadcn/ui、Zustand、Zod、原生 fetch 封裝、Biome、Knip、next-intl、TanStack Query、React Hook Form、nuqs
- **Phase 2（規模成長時）**：Recharts
- **Phase 3（正式上線時）**：Sentry、CSP 安全標頭

> **關於 `next-intl`**：本專案從 Phase 1 就導入多語系（en + zh-TW）。事後遷移需要把所有路由搬進 `[locale]/`，成本很高。

---

## 目錄結構

> 本專案採用 next-intl 多語系架構，URL 自動變成 `/en/dashboard`、`/zh-TW/dashboard`。

```
frontend/
├── src/
│   ├── app/                        # 1. 路由層 (Next.js App Router)
│   │   ├── layout.tsx              #    Root Layout（pass-through + 全域 metadata）
│   │   ├── globals.css             #    Tailwind 進入點 + shadcn/ui 色彩變數 + 自訂動畫
│   │   ├── [locale]/               #    next-intl 語系路段
│   │   │   ├── layout.tsx          #    語系 Layout（<html lang={locale}> + Providers）
│   │   │   ├── not-found.tsx       #    自訂 404 頁面
│   │   │   ├── (marketing)/        #    路由群組：公開頁面（Landing Page）
│   │   │   │   ├── page.tsx        #      首頁（產品介紹、功能特色）
│   │   │   │   ├── pricing/page.tsx#      定價方案
│   │   │   │   └── layout.tsx      #      含 Header + Footer
│   │   │   ├── (auth)/             #    路由群組：授權頁面
│   │   │   │   ├── login/page.tsx
│   │   │   │   ├── register/page.tsx
│   │   │   │   └── layout.tsx      #      極簡置中版面
│   │   │   └── (dashboard)/        #    路由群組：需登入的主控台
│   │   │       ├── overview/page.tsx    #  總覽（即時放貸狀態、收益摘要）
│   │   │       ├── api-keys/            #  API Key 管理
│   │   │       │   ├── page.tsx
│   │   │       │   ├── _components/
│   │   │       │   │   ├── api-key-form.tsx
│   │   │       │   │   └── api-key-list.tsx
│   │   │       │   └── actions.ts
│   │   │       ├── strategy/            #  策略參數設定
│   │   │       │   ├── page.tsx
│   │   │       │   ├── _components/
│   │   │       │   │   ├── strategy-form.tsx
│   │   │       │   │   └── parameter-group.tsx
│   │   │       │   └── actions.ts
│   │   │       ├── history/             #  執行記錄 + 帳單
│   │   │       │   ├── page.tsx
│   │   │       │   └── _components/
│   │   │       │       ├── execution-table.tsx
│   │   │       │       └── billing-table.tsx
│   │   │       ├── settings/page.tsx    #  帳戶設定
│   │   │       ├── loading.tsx          #  Dashboard Skeleton（頁面切換時顯示）
│   │   │       ├── error.tsx            #  Dashboard Error Boundary
│   │   │       └── layout.tsx           #  側邊欄 + 頂部列
│   │   ├── global-error.tsx        #    最頂層 Error Boundary
│   │   └── api/                    #    API Route Handlers（不需語系前綴）
│   │       ├── proxy/[...path]/route.ts #  同源代理：轉發至 Go 後端（解決跨域 cookie）
│   │       └── ws-token/route.ts        #  簽發短期 WebSocket 認證 token
│   │
│   ├── i18n/                        # 2. 國際化設定層 (next-intl)
│   │   ├── routing.ts              #    defineRouting：locales、defaultLocale
│   │   ├── request.ts              #    getRequestConfig：載入翻譯 JSON
│   │   └── navigation.ts           #    createNavigation：Link、redirect、useRouter
│   │
│   ├── components/                  # 3. 視圖層（純 UI，不含業務邏輯）
│   │   ├── ui/                     #    shadcn/ui 元件（Button、Input、Modal、DataTable）
│   │   ├── layout/                 #    佈局元件（Header、Footer、Sidebar、TopBar）
│   │   └── shared/                 #    跨頁面共用元件（PageTitle、EmptyState、StatusBadge）
│   │
│   ├── features/                    # 4. 功能模組層
│   │   ├── auth/                   #    登入模組
│   │   │   ├── components/         #       LoginForm、RegisterForm
│   │   │   ├── hooks/              #       useLogin、useCurrentUser
│   │   │   ├── api/                #       auth-api.ts
│   │   │   ├── types.ts
│   │   │   ├── validations.ts
│   │   │   └── index.ts
│   │   └── dashboard/              #    即時儀表板模組（跨頁面共用）
│   │       ├── components/         #       MarketOverview、LendingStatus、EarningsChart
│   │       ├── hooks/              #       useDashboardWS、useMarketSnapshot
│   │       ├── api/                #       dashboard-api.ts
│   │       ├── types.ts
│   │       └── index.ts
│   │
│   ├── lib/                         # 5. 核心工具層（無狀態純函式）
│   │   ├── api-client.ts           #    封裝 fetch（走 /api/proxy 同源代理）
│   │   ├── ws-client.ts            #    WebSocket 連接管理（自動重連、心跳）
│   │   ├── query-keys.ts           #    TanStack Query Key Factory（集中管理）
│   │   ├── validations.ts          #    共用 Zod Schema
│   │   ├── env.ts                  #    環境變數驗證（Zod）
│   │   ├── format.ts               #    利率格式化、貨幣格式化、日期格式化
│   │   └── utils.ts                #    cn()
│   │
│   ├── hooks/                       # 6. 共用 Hooks
│   │   ├── use-debounce.ts         #    防抖（搜尋框）
│   │   └── use-media-query.ts      #    螢幕寬度偵測
│   │
│   ├── store/                       # 7. 全域狀態層 (Zustand)
│   │   ├── use-ui-store.ts         #    側邊欄開關、主題
│   │   └── use-ws-store.ts         #    WebSocket 連線狀態、最新 MarketSnapshot
│   │
│   ├── types/                       # 8. 型別定義層
│   │   └── index.ts                #    User、ApiError、MarketSnapshot、StrategyConfig 等
│   │
│   └── providers/                   # 9. 狀態提供者層
│       └── query-provider.tsx      #    TanStack Query Provider + Devtools
│
├── messages/                        # 翻譯檔
│   ├── en.json
│   └── zh-TW.json
│
├── public/                          # 靜態資源
├── middleware.ts                    # next-intl 語系路由 + Auth 權限檢查
├── next.config.ts                   # Next.js 設定（next-intl plugin、安全標頭）
├── postcss.config.mjs              # PostCSS 設定
├── components.json                  # shadcn/ui 設定
├── biome.json                       # Biome 格式化 + Lint 設定
├── knip.config.ts                   # Knip 未使用程式碼檢測設定
├── .env.example                     # 環境變數範例
├── sentry.client.config.ts          # Sentry 客戶端設定（Phase 3）
├── sentry.server.config.ts          # Sentry 伺服器端設定（Phase 3）
└── package.json
```

### 判斷規則：功能程式碼放哪裡

| 情境 | 放哪裡 | 原因 |
|------|--------|------|
| 功能被 >= 2 個頁面使用 | `features/` | 避免重複程式碼 |
| 功能只有 1 個頁面使用 | 路由資料夾內（`_components/`） | 打開資料夾就是全部 |
| 純 UI 元件，無業務邏輯 | `components/shared/` | 跨功能共用 |
| 全站共用的狀態/工具 | `store/`、`lib/`、`hooks/` | 基礎設施層 |

> **起始建議**：先把功能程式碼放在路由資料夾內。當第二個頁面也需要用到時，再提取到 `features/`。

---

## 架構設計原則

### 1. 路由群組隔離版面

路由群組放在 `[locale]/` 下：

```
app/[locale]/
├── (marketing)/     → /en/, /zh-TW/pricing（Landing + 定價）
├── (auth)/          → /en/login, /zh-TW/register（極簡置中）
└── (dashboard)/     → /en/overview, /zh-TW/strategy（側邊欄 + 頂部列）
```

| 群組 | 版面 | URL 範例 |
|------|------|---------|
| `(marketing)` | Header + Footer | `/en/`、`/zh-TW/pricing` |
| `(auth)` | 極簡置中 | `/en/login`、`/zh-TW/register` |
| `(dashboard)` | 側邊欄 + 頂部列 | `/en/overview`、`/zh-TW/strategy` |

### 2. 三層狀態管理

| 狀態類型 | 工具 | 範例 |
|----------|------|------|
| 客戶端全域 | Zustand | UI 開關、主題、WebSocket 連線狀態、最新 MarketSnapshot |
| 伺服器資料 | TanStack Query | API Key 列表、策略參數、帳單記錄 |
| URL 狀態 | nuqs | 執行記錄過濾器、帳單日期範圍、分頁 |

**原則**：不要用 Zustand 存 API 資料（用 TanStack Query），不要用 useState 存搜尋參數（用 nuqs）。WebSocket 推送的即時數據用 Zustand（因為不是 request-response 模式）。

### 3. Server Actions vs API Routes

| 場景 | 推薦方式 | 原因 |
|------|----------|------|
| 表單送出（新增 API Key、修改策略） | Server Actions | 自動處理 revalidation |
| Client Component 資料抓取 | TanStack Query + API Client → `/api/proxy/*` | 同源代理，cookie 自動帶上 |
| WebSocket 連接 | 原生 WebSocket（`lib/ws-client.ts`） | 先透過 `/api/ws-token` 取得短期 token |
| Webhook 接收 | API Routes | 外部服務回呼需要固定 URL |

#### Server Action 內的 Auth 驗證

Middleware 保護的是「路由存取」，但 Server Action 可以被直接 POST 呼叫。**在 action 內部獨立驗證身份**：

```typescript
// app/[locale]/(dashboard)/api-keys/actions.ts
"use server";

import { cookies } from "next/headers";
import { revalidatePath } from "next/cache";
import { routing } from "@/i18n/routing";
import { apiKeySchema } from "./validations";

type ActionState = {
  success: boolean;
  errors?: Record<string, string[]>;
};

export async function createApiKey(
  _prevState: ActionState,
  formData: FormData,
): Promise<ActionState> {
  // 1. Auth 驗證（Server Action 可被直接 POST，不能只依賴 middleware）
  const token = (await cookies()).get("auth_token")?.value;
  if (!token) {
    return { success: false, errors: { auth: ["Unauthorized"] } };
  }

  // 2. 輸入驗證
  const parsed = apiKeySchema.safeParse(Object.fromEntries(formData));
  if (!parsed.success) {
    return { success: false, errors: parsed.error.flatten().fieldErrors };
  }

  // 3. 呼叫 Go 後端 API（Server Action 在 server-side 執行，直接打後端）
  const res = await fetch(`${process.env.API_URL}/api/v1/api-keys`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(parsed.data),
  });

  if (!res.ok) {
    const error = await res.json();
    return { success: false, errors: { api: [error.error.message] } };
  }

  // 4. Revalidate 所有 locale 的路徑（next-intl 的實際路徑帶有 locale 前綴）
  for (const locale of routing.locales) {
    revalidatePath(`/${locale}/api-keys`);
  }
  return { success: true };
}
```

---

## 與 Go 後端的整合 — 同源代理架構

### 為什麼需要同源代理

前端部署在 Vercel（`app.example.com`），Go 後端在另一台機器（`api.example.com`）。**跨域環境下**：

- HttpOnly cookie 不會被瀏覽器自動帶給不同 origin 的 API
- 瀏覽器 WebSocket API 不支援自訂 headers（無法手動帶 `Authorization`）
- 直接暴露後端 URL 給前端有安全風險

**解法**：所有 Client Component 的請求都打前端自己的 `/api/proxy/*`，由 Next.js API Route 在 server-side 讀取 HttpOnly cookie，附上 `Authorization` header 後轉發給 Go 後端。

```
Client Component → /api/proxy/api-keys (同源) → Go 後端 /api/v1/api-keys (server-side)
                   ↑ cookie 自動帶上              ↑ 讀取 cookie，轉為 Bearer token
```

### 同源代理 API Route (`app/api/proxy/[...path]/route.ts`)

```typescript
import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

const API_URL = process.env.API_URL!;

async function proxyRequest(request: NextRequest, params: Promise<{ path: string[] }>) {
  const { path } = await params;
  const targetPath = `/api/v1/${path.join("/")}`;
  const url = new URL(targetPath, API_URL);

  // 保留查詢參數
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.set(key, value);
  });

  // 從 HttpOnly cookie 讀取 token（server-side 可存取）
  const token = (await cookies()).get("auth_token")?.value;

  const headers = new Headers();
  headers.set("Content-Type", "application/json");
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const res = await fetch(url.toString(), {
    method: request.method,
    headers,
    body: request.method !== "GET" ? await request.text() : undefined,
  });

  const data = await res.text();
  return new NextResponse(data, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const DELETE = proxyRequest;
```

### API Client (`lib/api-client.ts`)

封裝原生 `fetch`，**Client Component 走同源代理**，**Server Component / Server Action 直接打後端**。

```typescript
// 使用方式（Client Component 中）
import { apiClient } from "@/lib/api-client";

// 取得策略參數 — get() 自動解包 data
const config = await apiClient.get<UserConfig>("/configs");

// 更新策略參數
const result = await apiClient.put<UserConfig>("/configs", newConfig);

// 取得帳單記錄 — getList() 保留 pagination
const bills = await apiClient.getList<BillingListResponse>("/billing", {
  params: { limit: "20" },
});
// 下一頁：apiClient.getList<BillingListResponse>("/billing", { params: { after: bills.pagination.nextCursor } })
```

**內建功能**：
- Client Component：所有請求自動加上 `/api/proxy` 前綴，cookie 同源自動帶上
- Server Component / Server Action：直接打 `API_URL`，手動附加 `Authorization` header
- JSON 自動解析 + 智慧解包（見下方說明）
- 統一的 `ApiError` 錯誤類別，對應後端格式 `{ "error": { "code": "...", "message": "..." } }`

**`data` 解包策略**：提供兩個方法區分單筆與分頁回應：
- **`get<T>(path)`** — 自動解包 `data`：後端回傳 `{ "data": T }`，呼叫端拿到 `T`
- **`getList<T>(path)`** — 不解包：後端回傳 `{ "data": T[], "pagination": {...} }`，呼叫端拿到完整結構

```typescript
// 單筆資源 — 用 get()，自動解包 data
const config = await apiClient.get<UserConfig>("/configs");
// config 的型別是 UserConfig

// 分頁列表 — 用 getList()，保留 pagination
const bills = await apiClient.getList<BillingListResponse>("/billing", {
  params: { limit: "20" },
});
// bills 的型別是 BillingListResponse { data: BillingRecord[], pagination: CursorPagination }
```

```typescript
// lib/api-client.ts 核心實作概念
const isServer = typeof window === "undefined";

function getBaseUrl() {
  if (isServer) {
    return process.env.API_URL + "/api/v1"; // Server-side 直連後端
  }
  return "/api/proxy"; // Client-side 走同源代理
}
```

### Cookie 安全旗標

登入成功後由 Server Action 設定 cookie：

| 旗標 | 值 | 用途 |
|------|------|------|
| `HttpOnly` | `true` | 防止 JS 讀取 cookie（XSS 防護核心） |
| `Secure` | `true` | 僅 HTTPS 傳輸 |
| `SameSite` | `Lax` | 防止 CSRF 攻擊 |
| `Path` | `/` | 全站可用 |
| `Max-Age` | `604800` | 7 天 |

> **為什麼用同源代理而非 `SameSite: None`**：`SameSite: None` 允許跨域帶 cookie，但會降低 CSRF 防護，且需要後端設定 CORS。同源代理讓前後端從瀏覽器角度看都是同一個 origin，安全性更好。

### WebSocket 認證 — 短期 Token 方案

瀏覽器 WebSocket API 不支援自訂 headers，無法直接帶 `Authorization`。解法是透過 API Route 簽發短期 token：

```
1. Client → GET /api/ws-token（同源，cookie 自動帶上）
2. Server 驗證 cookie → 向 Go 後端請求短期 token（有效期 30 秒）
3. Client → WebSocket ws://api.example.com/api/v1/ws/dashboard?token=xxx
4. Go 後端驗證 token → 建立 WebSocket 連接
```

#### WS Token API Route (`app/api/ws-token/route.ts`)

```typescript
import { cookies } from "next/headers";
import { NextResponse } from "next/server";

export async function GET() {
  const token = (await cookies()).get("auth_token")?.value;
  if (!token) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // 向 Go 後端請求短期 WS token
  const res = await fetch(`${process.env.API_URL}/api/v1/auth/ws-token`, {
    headers: { Authorization: `Bearer ${token}` },
  });

  if (!res.ok) {
    return NextResponse.json({ error: "Failed to get WS token" }, { status: res.status });
  }

  const body = await res.json();
  return NextResponse.json(body.data); // { token: "short-lived-token" }
}
```

#### WebSocket Client (`lib/ws-client.ts`)

```typescript
// lib/ws-client.ts 核心功能
// - 連接前先從 /api/ws-token 取得短期 token
// - 自動重連（指數退避），重連時重新取得 token
// - 心跳保活（ping/pong）
// - 連線狀態回報至 Zustand store
// - 解析 MarketSnapshot、LendingStatus 等即時事件
```

```typescript
// 使用方式（在 features/dashboard/hooks/use-dashboard-ws.ts）
export function useDashboardWS() {
  const setSnapshot = useWSStore((s) => s.setSnapshot);
  const setLendingStatus = useWSStore((s) => s.setLendingStatus);
  const setStatus = useWSStore((s) => s.setConnectionStatus);

  useEffect(() => {
    let ws: ReturnType<typeof createWSClient> | null = null;

    async function connect() {
      // 1. 先取得短期 token（同源請求，cookie 自動帶上）
      const res = await fetch("/api/ws-token");
      if (!res.ok) return;
      const { token } = await res.json();

      // 2. 用 token 建立 WebSocket 連接（直連 Go 後端）
      ws = createWSClient({
        url: `${process.env.NEXT_PUBLIC_WS_URL}/api/v1/ws/dashboard?token=${token}`,
        onMessage: (event) => {
          switch (event.type) {
            case "market_snapshot":
              setSnapshot(event.data);
              break;
            case "lending_status":
              setLendingStatus(event.data);
              break;
          }
        },
        onStatusChange: setStatus,
      });
    }

    connect();
    return () => ws?.close();
  }, [setSnapshot, setLendingStatus, setStatus]);
}
```

### 前後端 API 對應表

| 前端呼叫路徑 | 代理轉發至 | Method | 說明 |
|-------------|-----------|--------|------|
| `/api/proxy/health` | `/api/v1/health` | GET | 偵測後端是否存活 |
| `/api/proxy/auth/register` | `/api/v1/auth/register` | POST | 建立帳號 |
| `/api/proxy/auth/login` | `/api/v1/auth/login` | POST | JWT 簽發 → proxy 設定 HttpOnly cookie |
| `/api/proxy/me` | `/api/v1/me` | GET | 當前使用者資訊 |
| `/api/proxy/me/password` | `/api/v1/me/password` | PUT | 修改密碼 |
| `/api/proxy/api-keys` | `/api/v1/api-keys` | GET | 列出已綁定的 Bitfinex API Key |
| `/api/proxy/api-keys` | `/api/v1/api-keys` | POST | 加密儲存 + 驗證權限 |
| `/api/proxy/api-keys/:id` | `/api/v1/api-keys/:id` | GET | 查看單筆 API Key |
| `/api/proxy/api-keys/:id` | `/api/v1/api-keys/:id` | DELETE | 同時停止對應 Worker |
| `/api/proxy/api-keys/:id/verify` | `/api/v1/api-keys/:id/verify` | POST | 重新驗證 Bitfinex 權限 |
| `/api/proxy/configs` | `/api/v1/configs` | GET | 當前放貸策略設定 |
| `/api/proxy/configs` | `/api/v1/configs` | PUT | 熱載入至 Worker |
| `/api/proxy/configs` | `/api/v1/configs` | DELETE | 刪除策略設定 |
| `/api/proxy/dashboard` | `/api/v1/dashboard` | GET | 儀表板總覽（wallet + offers + credits + market） |
| `/api/proxy/earnings` | `/api/v1/earnings` | GET | 收益摘要 |
| `/api/proxy/executions` | `/api/v1/executions` | GET | 放貸執行歷史（cursor pagination） |
| `/api/proxy/billing` | `/api/v1/billing` | GET | 帳單記錄（cursor pagination） |
| `/api/proxy/billing/plan` | `/api/v1/billing/plan` | GET | 當前訂閱方案功能 |
| `/api/ws-token` | `/api/v1/auth/ws-token` | GET | 簽發短期 WS token |
| *直連* `WS_URL` | `/api/v1/ws/dashboard?token=xxx` | WebSocket | 即時推送 |

> **登入流程注意**：proxy 收到 Go 後端回傳的 JWT 後，需要在 proxy 層將 token 設定為 HttpOnly cookie 再回傳給前端，而非直接把 token 暴露給 Client。也可以改用 Server Action 處理登入流程。

---

## 核心模組實作規範

### `cn()` 工具函式 (`lib/utils.ts`)

```typescript
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```

### 格式化工具 (`lib/format.ts`)

放貸平台專用的格式化函式：

```typescript
// 年化利率格式化（Bitfinex 用日利率，需轉換）
export function formatAPR(dailyRate: number): string {
  return `${(dailyRate * 365 * 100).toFixed(2)}%`;
}

// 利率格式化（每日）
export function formatDailyRate(rate: number): string {
  return `${(rate * 100).toFixed(4)}%`;
}

// USD 金額格式化
export function formatUSD(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(amount);
}

// 放貸天數格式化
export function formatPeriod(days: number): string {
  return `${days}d`;
}
```

### Zustand — WebSocket 狀態 (`store/use-ws-store.ts`)

```typescript
import { create } from "zustand";
import type { MarketSnapshot, LendingStatus } from "@/types";

type ConnectionStatus = "connecting" | "connected" | "disconnected" | "error";

interface WSState {
  status: ConnectionStatus;
  snapshot: MarketSnapshot | null;
  lendingStatus: LendingStatus | null;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setSnapshot: (snapshot: MarketSnapshot) => void;
  setLendingStatus: (status: LendingStatus) => void;
}

export const useWSStore = create<WSState>()((set) => ({
  status: "disconnected",
  snapshot: null,
  lendingStatus: null,
  setConnectionStatus: (status) => set({ status }),
  setSnapshot: (snapshot) => set({ snapshot }),
  setLendingStatus: (lendingStatus) => set({ lendingStatus }),
}));
```

### TanStack Query — Query Key Factory

```typescript
// lib/query-keys.ts — 集中管理所有 Query Key
export const apiKeyKeys = {
  all: ["api-keys"] as const,
  list: () => [...apiKeyKeys.all, "list"] as const,
};

export const configKeys = {
  all: ["configs"] as const,
  current: () => [...configKeys.all, "current"] as const,
};

export const billingKeys = {
  all: ["billing"] as const,
  list: (params?: BillingParams) => [...billingKeys.all, "list", params] as const,
};

export const executionKeys = {
  all: ["executions"] as const,
  list: (params?: ExecutionParams) => [...executionKeys.all, "list", params] as const,
};
```

### QueryProvider (`providers/query-provider.tsx`)

```typescript
"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { useState } from "react";

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60 * 1000,
            retry: 1,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      {children}
      <ReactQueryDevtools initialIsOpen={false} />
    </QueryClientProvider>
  );
}
```

> **重要**：不要在模組層級 `const queryClient = new QueryClient()` — 會導致 SSR 時所有請求共用同一個快取，造成跨使用者資料洩漏。

### Zod 驗證 (`lib/validations.ts`)

```typescript
import { z } from "zod";

// API Key 表單驗證 — 錯誤訊息統一用英文 key，在表單元件層用 t() 翻譯
export const apiKeySchema = z.object({
  label: z.string().min(1, "required").max(50, "maxLength"),
  apiKey: z.string().min(1, "required"),
  apiSecret: z.string().min(1, "required"),
});

export type ApiKeyFormInput = z.infer<typeof apiKeySchema>;
```

> **Zod v4 注意事項**：v4 的 `z.object()` 預設為 strict mode（不允許多餘屬性），效能大幅提升（解析速度快 2-7 倍）。

> **多語系驗證訊息**：Zod schema 的錯誤訊息用英文 key（如 `"required"`），在表單元件中透過 `t(`validation.${error}`)` 翻譯。避免 schema 中寫死特定語言的文字。翻譯檔加上 `"validation": { "required": "This field is required" }` / `"validation": { "required": "此欄位為必填" }`。

---

## 型別定義 (`types/index.ts`)

對應 Go 後端 `domain/` 的型別。後端 JSON 一律使用 camelCase，前端型別直接對應，無需轉換。

後端所有成功回應統一用 `{ "data": ... }` 包裝，錯誤回應用 `{ "error": ... }` 包裝。`api-client.ts` 自動解包 `data`，呼叫端拿到的就是業務資料。

```typescript
// 通用回應包裝（後端統一格式）
export interface ApiResponse<T> {
  data: T;
}

// 錯誤回應（後端統一格式）
export interface ApiError {
  error: {
    code: string;
    message: string;
  };
}

// Cursor-based 分頁（後端使用 cursor-based pagination）
export interface CursorPagination {
  nextCursor?: string;
  hasMore: boolean;
}

// 使用者
export interface User {
  id: string;
  email: string;
  status: "active" | "suspended";
  plan: "free" | "starter" | "pro" | "enterprise";
  createdAt: string;
  updatedAt: string;
}

// API Key（對應後端 handler/apikey.go Create/List/GetByID 回傳格式）
export interface ApiKey {
  id: string;
  label: string;
  apiKey: string;          // 完整 API Key
  apiSecret: string;       // 永遠是 "****"（後端 mask 處理）
  exchangeStatus: string;  // "verified" | "unverified" | "failed"
  createdAt: string;
  fundingBalance?: {       // 可選，僅 Create 時回傳
    currency: string;
    balance: number;
    available: number;
  };
}

// API Key 驗證結果（對應後端 handler/apikey.go Verify 回傳格式）
export interface VerifyResult {
  status: string;          // "verified" | "failed"
  error?: string;
  fundingBalance?: {
    currency: string;
    balance: number;
    available: number;
  };
}

// 策略參數（對應後端 domain.StrategyConfig）
export interface AmountConfig {
  min: number;
  max: number;
}

export interface RateConfig {
  min: number;
  max: number;
}

export interface PeriodConfig {
  min: number;
  max: number;
}

export interface StrategyConfig {
  currency: string;
  amount: AmountConfig;
  rate: RateConfig;
  period: PeriodConfig;
  autoRenew: boolean;
}

// UserConfig（對應後端 handler/config.go 回傳格式）
export interface UserConfig {
  id: string;
  userId: string;
  config: StrategyConfig;
  createdAt: string;
  updatedAt: string;
}

// ── Dashboard 相關（REST GET /dashboard）──

// 錢包摘要
export interface WalletSummary {
  currency: string;
  balance: number;
  balanceAvailable: number;
}

// 掛單摘要
export interface OfferSummary {
  id: number;
  currency: string;
  amount: number;
  rate: number;
  period: number;
  status: string;
  createdAt: string;
}

// 債權摘要
export interface CreditSummary {
  id: number;
  currency: string;
  amount: number;
  rate: number;
  period: number;
  status: string;
  autoRenew: boolean;
  openedAt: string;
}

// 市場摘要
export interface MarketSummary {
  frr: number;
  regime: string;
  mdcScore: number;
  flashFreeze: boolean;
  timestamp: string;
}

// Dashboard 總覽（對應後端 service.DashboardSummary）
export interface DashboardSummary {
  wallet: WalletSummary | null;
  offers: OfferSummary[];
  credits: CreditSummary[];
  market: MarketSummary | null;
  engineReady: boolean;
}

// ── Earnings（REST GET /earnings）──

export interface EarningsSummary {
  estimatedDailyEarning: number;
  weightedAPY: number;
  earnings7d: number;
  earnings30d: number;
  totalLent: number;
  activeCredits: number;
  currency: string;
}

// ── WebSocket 推送型別（Phase F12-F13，尚未實作）──

export interface MarketSnapshot {
  frr: number;
  regime: string;
  mdcScore: number;
  flashFreeze: boolean;
  timestamp: string;
}

export interface LendingStatus {
  activeOffers: number;
  activeCredits: number;
  totalLent: number;
  availableBalance: number;
  estimatedDailyEarning: number;
  workerStatus: "running" | "paused" | "stopped";
}

// ── 帳單記錄（對應後端 domain.BillingRecord）──

export interface BillingRecord {
  id: string;
  userId: string;
  periodStart: string;
  periodEnd: string;
  plan: string;
  amount: number;
  currency: string;
  status: "pending" | "paid" | "overdue" | "waived";
  paidAt?: string;
  createdAt: string;
}

// 帳單分頁回應（後端回傳 { data: [...], pagination: {...} }）
export interface BillingListResponse {
  data: BillingRecord[];
  pagination: CursorPagination;
}

// 訂閱方案功能（GET /billing/plan）
export interface PlanFeatures {
  plan: string;
  price: number;
  autoLending: boolean;
  advancedStrategy: boolean;
  emailNotify: boolean;
  priorityQuota: boolean;
  customParams: boolean;
}

// ── 執行記錄（對應後端 domain.ExecutionRecord）──

export interface ExecutionRecord {
  id: string;
  userId: string;
  action: "place" | "cancel" | "filled" | "renew";
  currency: string;
  amount: number;
  rate: number;
  period: number;
  offerId?: number;
  status: string;
  errorMessage?: string;
  createdAt: string;
}

// 執行記錄分頁回應（後端回傳 { data: [...], pagination: {...} }）
export interface ExecutionListResponse {
  data: ExecutionRecord[];
  pagination: CursorPagination;
}

// 引擎狀態（GET /health 回傳的 engine 欄位）
export interface EngineStatus {
  running: boolean;
  workerCount: number;
}
```

---

## 樣式系統 — Tailwind CSS v4 + shadcn/ui

### shadcn/ui 色彩變數系統

本專案採用暗色主題（金融交易介面慣例）：

```css
/* globals.css — 暗色金融風格 */
@layer utilities {
  :root {
    --background: 220 20% 6%;         /* 深藍黑 */
    --foreground: 210 20% 92%;        /* 淺灰白 */
    --primary: 142 70% 45%;           /* 綠色（收益、上漲） */
    --destructive: 0 72% 51%;         /* 紅色（虧損、下跌） */
    --accent: 217 91% 60%;            /* 藍色（互動元素） */
    --muted: 220 15% 14%;
    --muted-foreground: 220 10% 55%;
    --card: 220 18% 9%;
    --border: 220 15% 18%;
    --radius: 0.5rem;
  }
}

@theme {
  --color-background: hsl(var(--background));
  --color-foreground: hsl(var(--foreground));
  --color-primary: hsl(var(--primary));
  --color-destructive: hsl(var(--destructive));
  --color-accent: hsl(var(--accent));
  --color-card: hsl(var(--card));
  /* ... */
}
```

```css
@custom-variant dark (&:is(.dark *));
```

### PostCSS 設定

```javascript
/** @type {import('postcss-load-config').Config} */
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
```

---

## 國際化 (i18n) — next-intl 完整設定

### 設定檔（4 個檔案）

#### 1. `src/i18n/routing.ts`

```typescript
import { defineRouting } from "next-intl/routing";

export const routing = defineRouting({
  locales: ["en", "zh-TW"],
  defaultLocale: "en",
});
```

#### 2. `src/i18n/request.ts`

```typescript
import { getRequestConfig } from "next-intl/server";
import { hasLocale } from "next-intl";
import { routing } from "./routing";

export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale;
  const locale = hasLocale(routing.locales, requested)
    ? requested
    : routing.defaultLocale;

  return {
    locale,
    messages: (await import(`../../messages/${locale}.json`)).default,
  };
});
```

#### 3. `src/i18n/navigation.ts`

```typescript
import { createNavigation } from "next-intl/navigation";
import { routing } from "./routing";

export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation(routing);
```

#### 4. `middleware.ts`

```typescript
import createMiddleware from "next-intl/middleware";
import { routing } from "@/i18n/routing";
import { NextRequest, NextResponse } from "next/server";

const intlMiddleware = createMiddleware(routing);

const localePrefix = new RegExp(`^/(${routing.locales.join("|")})`);

const protectedPaths = ["/overview", "/api-keys", "/strategy", "/history", "/settings"];
const authPaths = ["/login", "/register"];

export default function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const pathnameWithoutLocale = pathname.replace(localePrefix, "") || "/";

  const token = request.cookies.get("auth_token")?.value;
  const isProtected = protectedPaths.some((p) => pathnameWithoutLocale.startsWith(p));
  const isAuthPage = authPaths.some((p) => pathnameWithoutLocale.startsWith(p));

  if (isProtected && !token) {
    const locale = pathname.match(localePrefix)?.[1] || routing.defaultLocale;
    const loginUrl = new URL(`/${locale}/login`, request.url);
    loginUrl.searchParams.set("callbackUrl", pathnameWithoutLocale);
    return NextResponse.redirect(loginUrl);
  }

  if (isAuthPage && token) {
    const locale = pathname.match(localePrefix)?.[1] || routing.defaultLocale;
    return NextResponse.redirect(new URL(`/${locale}/overview`, request.url));
  }

  return intlMiddleware(request);
}

export const config = {
  matcher: ["/((?!api|_next|.*\\..*).*)"],
};
```

### `next.config.ts`

```typescript
import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const nextConfig: NextConfig = {
  // 圖片白名單（如需要）
  // images: { remotePatterns: [...] },
};

const withNextIntl = createNextIntlPlugin();

export default withNextIntl(nextConfig);

// Phase 3: Sentry 錯誤監控
// import { withSentryConfig } from "@sentry/nextjs";
// export default withSentryConfig(withNextIntl(nextConfig), {
//   org: process.env.SENTRY_ORG,
//   project: process.env.SENTRY_PROJECT,
//   silent: !process.env.CI,
//   widenClientFileUpload: true,
//   disableLogger: true,
//   automaticVercelMonitors: true,
// });
```

### `src/app/layout.tsx` — Root Layout（pass-through）

```tsx
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "BFX Funding Bot",
  description: "Bitfinex 自動放貸 SaaS 平台",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return children;
}
```

### `src/app/[locale]/layout.tsx` — 語系 Layout

```tsx
import { NextIntlClientProvider, hasLocale } from "next-intl";
import { notFound } from "next/navigation";
import { setRequestLocale } from "next-intl/server";
import { Inter } from "next/font/google";
import { routing } from "@/i18n/routing";
import { QueryProvider } from "@/providers/query-provider";
import { NuqsAdapter } from "nuqs/adapters/next/app";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

type Props = {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
};

export function generateStaticParams() {
  return routing.locales.map((locale) => ({ locale }));
}

export default async function LocaleLayout({ children, params }: Props) {
  const { locale } = await params;

  if (!hasLocale(routing.locales, locale)) {
    notFound();
  }

  setRequestLocale(locale);

  return (
    <html lang={locale} className={`${inter.variable} dark`}>
      <body className="font-sans antialiased bg-background text-foreground">
        <NextIntlClientProvider>
          <QueryProvider>
            <NuqsAdapter>
              {children}
            </NuqsAdapter>
          </QueryProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
```

### 翻譯檔結構 (`messages/`)

```json
// messages/en.json
{
  "nav": {
    "overview": "Overview",
    "apiKeys": "API Keys",
    "strategy": "Strategy",
    "history": "History",
    "settings": "Settings",
    "logout": "Logout"
  },
  "common": {
    "loading": "Loading...",
    "error": "Something went wrong",
    "save": "Save",
    "cancel": "Cancel",
    "delete": "Delete",
    "confirm": "Confirm",
    "back": "Back"
  },
  "auth": {
    "login": "Login",
    "register": "Register",
    "email": "Email",
    "password": "Password",
    "forgotPassword": "Forgot password?",
    "noAccount": "Don't have an account?",
    "hasAccount": "Already have an account?"
  },
  "overview": {
    "title": "Dashboard",
    "totalLent": "Total Lent",
    "availableBalance": "Available Balance",
    "activeOffers": "Active Offers",
    "activeCredits": "Active Credits",
    "estimatedDailyEarning": "Est. Daily Earning",
    "workerStatus": "Worker Status",
    "marketSnapshot": "Market Snapshot",
    "regime": "Market Regime",
    "mdc": "Market Demand Curve"
  },
  "apiKeys": {
    "title": "API Key Management",
    "addKey": "Add API Key",
    "label": "Label",
    "apiKey": "API Key",
    "apiSecret": "API Secret",
    "permissions": "Permissions",
    "deleteConfirm": "This will stop the lending worker and remove the API key. Are you sure?",
    "verifying": "Verifying permissions..."
  },
  "strategy": {
    "title": "Strategy Configuration",
    "signalParams": "Signal Parameters",
    "executionParams": "Execution Parameters",
    "periodParams": "Period Parameters",
    "safetyParams": "Safety Parameters",
    "saveSuccess": "Strategy updated. Worker will reload on next heartbeat.",
    "resetDefault": "Reset to Default"
  },
  "history": {
    "title": "History",
    "executions": "Execution Records",
    "billing": "Billing",
    "action": "Action",
    "amount": "Amount",
    "rate": "Rate",
    "period": "Period",
    "status": "Status",
    "time": "Time",
    "totalEarnings": "Total Earnings",
    "platformFee": "Platform Fee",
    "netEarnings": "Net Earnings"
  },
  "validation": {
    "required": "This field is required",
    "maxLength": "Exceeds maximum length",
    "invalidEmail": "Please enter a valid email"
  },
  "status": {
    "running": "Running",
    "paused": "Paused",
    "stopped": "Stopped",
    "connected": "Connected",
    "disconnected": "Disconnected"
  }
}
```

```json
// messages/zh-TW.json
{
  "nav": {
    "overview": "總覽",
    "apiKeys": "API 金鑰",
    "strategy": "策略設定",
    "history": "歷史記錄",
    "settings": "帳戶設定",
    "logout": "登出"
  },
  "common": {
    "loading": "載入中...",
    "error": "發生錯誤",
    "save": "儲存",
    "cancel": "取消",
    "delete": "刪除",
    "confirm": "確認",
    "back": "返回"
  },
  "auth": {
    "login": "登入",
    "register": "註冊",
    "email": "電子郵件",
    "password": "密碼",
    "forgotPassword": "忘記密碼？",
    "noAccount": "還沒有帳號？",
    "hasAccount": "已經有帳號？"
  },
  "overview": {
    "title": "儀表板",
    "totalLent": "總借出金額",
    "availableBalance": "可用餘額",
    "activeOffers": "進行中掛單",
    "activeCredits": "進行中債權",
    "estimatedDailyEarning": "預估日收益",
    "workerStatus": "Worker 狀態",
    "marketSnapshot": "市場快照",
    "regime": "市場體制",
    "mdc": "市場需求曲線"
  },
  "apiKeys": {
    "title": "API 金鑰管理",
    "addKey": "新增 API 金鑰",
    "label": "標籤",
    "apiKey": "API Key",
    "apiSecret": "API Secret",
    "permissions": "權限",
    "deleteConfirm": "這將停止放貸 Worker 並移除 API 金鑰，確定要刪除嗎？",
    "verifying": "驗證權限中..."
  },
  "strategy": {
    "title": "策略設定",
    "signalParams": "信號參數",
    "executionParams": "執行參數",
    "periodParams": "天數參數",
    "safetyParams": "安全參數",
    "saveSuccess": "策略已更新，Worker 將在下次心跳載入新參數。",
    "resetDefault": "恢復預設值"
  },
  "history": {
    "title": "歷史記錄",
    "executions": "執行記錄",
    "billing": "帳單",
    "action": "操作",
    "amount": "金額",
    "rate": "利率",
    "period": "天數",
    "status": "狀態",
    "time": "時間",
    "totalEarnings": "總收益",
    "platformFee": "平台費用",
    "netEarnings": "淨收益"
  },
  "validation": {
    "required": "此欄位為必填",
    "maxLength": "超過最大長度限制",
    "invalidEmail": "請輸入有效的電子郵件"
  },
  "status": {
    "running": "運行中",
    "paused": "已暫停",
    "stopped": "已停止",
    "connected": "已連線",
    "disconnected": "已斷線"
  }
}
```

### 在元件中使用

```tsx
// Server Component
import { getTranslations } from "next-intl/server";

export default async function OverviewPage() {
  const t = await getTranslations("overview");
  return <h1>{t("title")}</h1>;
}
```

```tsx
// Client Component
"use client";
import { useTranslations } from "next-intl";

export function WorkerStatusBadge({ status }: { status: string }) {
  const t = useTranslations("status");
  return <span>{t(status)}</span>;
}
```

```tsx
// 語系感知的連結
import { Link } from "@/i18n/navigation";
<Link href="/overview">Dashboard</Link>
```

### 新增語系 SOP

1. 建立 `messages/ja.json`（複製 `en.json` 翻譯）
2. 在 `src/i18n/routing.ts` 的 `locales` 加入 `"ja"`
3. 完成 — middleware 自動從 `routing.ts` 讀取，URL 自動支援 `/ja/overview`

---

## 程式碼品質 — Biome + Knip

### Biome 設定

```json
{
  "$schema": "https://biomejs.dev/schemas/2.4.5/schema.json",
  "files": {
    "includes": [
      "**/src/**/*.ts",
      "**/src/**/*.tsx",
      "!**/dist",
      "!**/.next",
      "!**/node_modules"
    ]
  },
  "formatter": {
    "enabled": true,
    "indentStyle": "space"
  },
  "assist": { "actions": { "source": { "organizeImports": "on" } } },
  "linter": {
    "enabled": true,
    "rules": {
      "recommended": true,
      "correctness": {
        "noUnusedVariables": "off"
      },
      "a11y": {
        "useAltText": "off",
        "useKeyWithClickEvents": "off",
        "useButtonType": "off"
      },
      "performance": {
        "noImgElement": "warn"
      }
    }
  },
  "javascript": {
    "formatter": {
      "quoteStyle": "double"
    }
  }
}
```

### package.json scripts

```json
{
  "scripts": {
    "dev": "next dev --turbopack",
    "build": "next build",
    "start": "next start",
    "lint": "tsc --noEmit && biome check src/",
    "format": "biome format --write",
    "knip": "knip"
  }
}
```

---

## 測試策略

### 核心原則

Next.js App Router 的非同步伺服器元件 (Async Server Components) 目前在 Vitest / Jest 的支援度不完善，官方建議 Async Components 直接用 E2E 測試覆蓋。因此採用 **20/80 策略**：

| 層級 | 工具 | 佔比 | 測試範圍 |
|------|------|------|----------|
| Unit | Vitest + React Testing Library | 20% | 核心商業邏輯（純函式） |
| E2E | Playwright | 80% | 使用者主流程（頁面互動） |

### Vitest — 單元測試（20%）

只測最核心、絕對不能算錯的商業邏輯：

```typescript
// __tests__/lib/format.test.ts
import { describe, expect, it } from "vitest";
import { formatAPR, formatDailyRate, formatUSD, formatPeriod } from "@/lib/format";

describe("formatAPR", () => {
  it("converts daily rate to annualized percentage", () => {
    expect(formatAPR(0.0001)).toBe("3.65%");
  });
});
```

**測試對象**（白名單，其餘不測）：
- `lib/format.ts` — 利率計算、貨幣格式化
- `lib/query-keys.ts` — Query Key 結構正確性
- `lib/api-client.ts` — ApiError 建構、URL 組合邏輯

**不測的東西**：
- UI 排版元件（shadcn/ui wrapper）
- Server Components（交給 E2E）
- 第三方套件的行為（next-intl、zustand）

### Playwright — E2E 測試（80%）

3-5 個腳本覆蓋使用者主流程：

| 腳本 | 覆蓋流程 |
|------|----------|
| `auth.spec.ts` | 註冊 → 登入 → 登出 → 未登入重導向 |
| `api-keys.spec.ts` | 新增 API Key → 驗證連線 → 刪除 |
| `strategy.spec.ts` | 修改放貸參數 → 儲存 → 確認生效 |
| `history.spec.ts` | 查看執行記錄 → 分頁翻頁 → 查看帳單 |
| `i18n.spec.ts` | 切換語系 (en ↔ zh-TW) → 確認翻譯 |

**E2E 全綠 = 可安心推上 Vercel + Koyeb。**

### package.json 測試指令

```json
{
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest",
    "test:e2e": "playwright test",
    "test:e2e:ui": "playwright test --ui"
  }
}
```

---

## Vercel 部署

### 部署流程

透過 Git 整合自動部署：在 Vercel Dashboard 連結 GitHub repo，push 到 `main` 時自動 build + deploy。

```bash
# 本地預覽（production build）
pnpm build && pnpm start
```

### Vercel 環境變數

在 Vercel Dashboard > Settings > Environment Variables 設定：

- **`NEXT_PUBLIC_*` 變數**：build 時注入，前後端皆可用
- **Server-only 變數**（`API_URL`、`AUTH_SECRET`）：僅 server-side 可存取，不暴露給前端
- **分環境設定**：可為 Production / Preview / Development 設定不同值

### Vercel 優勢

- **Image Optimization**：`next/image` 自動優化，零設定
- **ISR**：原生支援 Incremental Static Regeneration
- **Server Actions**：原生支援，無相容性問題
- **Sentry 整合**：`automaticVercelMonitors: true` 自動追蹤 Serverless Function 效能
- **Preview Deployments**：每個 PR 自動部署預覽環境

---

## 安全性

### Content Security Policy (next.config.ts)

```typescript
async headers() {
  if (process.env.NODE_ENV === "development") return [];

  return [
    {
      source: "/(.*)",
      headers: [
        {
          key: "Content-Security-Policy",
          value: [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",  // Next.js hydration 需要 inline script；進階可改用 nonce 機制
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self'",
            `connect-src 'self' ${process.env.NEXT_PUBLIC_WS_URL}`,  // REST API 走同源代理，只需允許 WS URL
            "frame-ancestors 'none'",
          ].join("; "),
        },
        { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" },
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "X-Frame-Options", value: "DENY" },
        { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
      ],
    },
  ];
}
```

> **`connect-src`**：必須允許 Go 後端 WebSocket URL，否則 WS 連線會被擋。REST API 走同源代理 `/api/proxy/*`，屬於 `'self'`，不需要額外允許。

### `.env.example`

```bash
# App
NEXT_PUBLIC_APP_URL=http://localhost:3000
NEXT_PUBLIC_APP_NAME="BFX Funding Bot"

# Go Backend API
NEXT_PUBLIC_WS_URL=ws://localhost:8080   # Client-side WebSocket 直連（僅 WS 需要暴露）
API_URL=http://localhost:8080            # Server-side only（代理 + Server Actions 用，不暴露給前端）

# Auth
AUTH_SECRET=your-auth-secret-here

# Sentry (Phase 3)
# NEXT_PUBLIC_SENTRY_DSN=
# SENTRY_ORG=
# SENTRY_PROJECT=
# SENTRY_AUTH_TOKEN=
```

### 環境變數驗證 (`lib/env.ts`)

```typescript
import { z } from "zod";

// Client-side 環境變數（build 時注入，前後端皆可用）
// 注意：API_URL 不需要 NEXT_PUBLIC_ 前綴，因為 REST API 走同源代理 /api/proxy/*
const clientEnvSchema = z.object({
  NEXT_PUBLIC_APP_URL: z.string().url(),
  NEXT_PUBLIC_APP_NAME: z.string().min(1),
  NEXT_PUBLIC_WS_URL: z.string().min(1),   // 僅 WebSocket 需要暴露給 Client
});

// Server-side 環境變數（僅 server 可用）
const serverEnvSchema = clientEnvSchema.extend({
  API_URL: z.string().url(),
  AUTH_SECRET: z.string().min(1),
});

// Client Component 中只驗證 NEXT_PUBLIC_* 變數
export const env = typeof window === "undefined"
  ? serverEnvSchema.parse(process.env)
  : clientEnvSchema.parse({
      NEXT_PUBLIC_APP_URL: process.env.NEXT_PUBLIC_APP_URL,
      NEXT_PUBLIC_APP_NAME: process.env.NEXT_PUBLIC_APP_NAME,
      NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL,
    });
```

> **Vercel 環境變數**：在 Vercel Dashboard 設定的環境變數會自動注入 `process.env`。Server-only 變數（`API_URL`、`AUTH_SECRET`）不加 `NEXT_PUBLIC_` 前綴，確保不會暴露給前端 bundle。缺少必要變數時，應用啟動就會報錯（fail fast）。

---

## Dashboard 頁面設計

### Overview 頁面佈局

```
┌─────────────────────────────────────────────────────────┐
│  TopBar: [Logo] BFX Funding Bot    [語系切換] [用戶頭像] │
├──────────┬──────────────────────────────────────────────┤
│          │                                              │
│ Sidebar  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐       │
│          │  │總借出 │ │可用額│ │日收益 │ │Worker│       │
│ 總覽     │  │$50,000│ │$5,000│ │$12.50│ │運行中│       │
│ API 金鑰 │  └──────┘ └──────┘ └──────┘ └──────┘       │
│ 策略設定 │                                              │
│ 歷史記錄 │  ┌─────────────────────────────────────┐    │
│ 帳戶設定 │  │  利率走勢圖（Recharts）               │    │
│          │  │  [24h] [7d] [30d]                    │    │
│          │  └─────────────────────────────────────┘    │
│          │                                              │
│          │  ┌────────────────┐ ┌────────────────┐      │
│          │  │ 進行中掛單      │ │ 市場快照        │      │
│          │  │ Offer 1: ...   │ │ MDC: 0.73      │      │
│          │  │ Offer 2: ...   │ │ Regime: Bull   │      │
│          │  └────────────────┘ └────────────────┘      │
│          │                                              │
├──────────┴──────────────────────────────────────────────┤
│  [WS: Connected]                              [v1.0.0]  │
└─────────────────────────────────────────────────────────┘
```

### 連線狀態指示器

WebSocket 連線狀態在 Dashboard Layout 的底部或頂部列顯示，讓使用者隨時知道即時數據是否正常推送。
