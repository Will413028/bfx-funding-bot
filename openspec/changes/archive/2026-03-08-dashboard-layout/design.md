## Context

Dashboard 是使用者登入後的主要介面。架構文件定義了「側邊欄 + 頂部列」的佈局，ASCII 圖示明確顯示 TopBar 橫跨全寬、Sidebar 在左側固定、Main 內容區域在右側。需要支援 mobile responsive（sidebar 收合為 drawer）。

## Goals / Non-Goals

**Goals:**
- Desktop: 固定 sidebar (w-64) + topbar + scrollable main area
- Mobile: sidebar 收合為 Sheet drawer，topbar 顯示 hamburger menu
- 語系切換（en ↔ zh-TW）整合在 topbar
- 套用 ui-premium-dark-theme 暗黑質感
- Nav items active 狀態高亮（用 usePathname 比對）
- Logout 按鈕在 sidebar 底部

**Non-Goals:**
- 不實作 WebSocket 連線狀態指示器（F12 範疇）
- 不實作使用者頭像/個人資料（未來增強）
- 不實作 sidebar collapse/expand toggle（MVP 保持固定寬度）

## Decisions

### D1: Sidebar 為 Server Component + Client nav items

Sidebar 容器是 Server Component，內部的 nav items 需要 `usePathname` 偵測 active 狀態，拆為 `SidebarNav` client component。

### D2: Mobile sidebar 用 shadcn Sheet

`<Sheet>` 提供現成的 slide-in drawer 行為 + 背景遮罩 + 動畫，不需手寫。在 `md:` breakpoint 切換：desktop 顯示固定 sidebar，mobile 隱藏改用 Sheet。

### D3: 語系切換用 DropdownMenu

DropdownMenu trigger 顯示當前語系，點開後列出所有可用語系。切換時用 `useRouter` + `usePathname` 替換 locale segment。

### D4: ui-premium-dark-theme 套用

- Sidebar: `bg-zinc-950 border-r border-white/5`
- TopBar: `bg-zinc-950/80 backdrop-blur-xl border-b border-white/5`（毛玻璃效果）
- Nav active: `bg-white/[0.08] text-foreground`
- Nav hover: `hover:bg-white/[0.04]`
- Main area: 預設 `bg-background`（已在 globals.css 設定）

### D5: 佈局結構

```
<div class="flex h-screen">
  <!-- Desktop Sidebar (hidden on mobile) -->
  <aside class="hidden md:flex w-64 flex-col">
    <SidebarNav />
  </aside>

  <div class="flex flex-1 flex-col">
    <!-- TopBar -->
    <header class="h-14 border-b">
      <!-- Mobile: hamburger + Sheet -->
      <!-- Logo + LocaleSwitcher + UserMenu -->
    </header>

    <!-- Main Content -->
    <main class="flex-1 overflow-y-auto p-6">
      {children}
    </main>
  </div>
</div>
```

## Risks / Trade-offs

- [Sheet 元件需要額外 radix 依賴] → shadcn CLI 自動安裝，pnpm-lock 會增大
- [語系切換全頁重新整理] → 使用 next-intl 的 `useRouter` + `usePathname`，只替換 locale segment，Next.js 會做 soft navigation
