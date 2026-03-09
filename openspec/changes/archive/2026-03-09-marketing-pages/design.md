## Context

Marketing 頁面是公開頁面（不需登入），用於吸引訪客並引導註冊。Landing page 介紹產品價值，Pricing page 展示方案差異。這些頁面是靜態內容，不需要 API 呼叫，可以是 Server Component（SEO 友好）。

## Goals / Non-Goals

**Goals:**
- Landing page: hero section + 4 功能特色卡片 + 底部 CTA
- Pricing page: 4 方案比較表（free/starter/pro/enterprise）
- Marketing layout: 簡潔 header（logo + Pricing 連結 + Login 按鈕）+ footer（copyright）
- premium dark theme 風格一致
- Server Component（靜態渲染，SEO 友好）
- i18n 支援（next-intl）

**Non-Goals:**
- 不實作 FAQ section
- 不實作 blog / changelog
- 不實作動畫（framer-motion）
- 不實作 Stripe 付款整合

## Decisions

### D1: Server Component

Marketing 頁面是純靜態內容，用 Server Component + `getTranslations` 支援 i18n。不需要 "use client"。

### D2: 方案資料 hardcoded

4 個方案的價格和功能清單直接寫在元件裡（來自 ROADMAP.md），不需從 API 取得。未來有 admin panel 時再考慮動態化。

### D3: (marketing) route group

使用 Next.js route group `(marketing)` 與 `(dashboard)`、`(auth)` 並列，共用 `[locale]` 但各自有獨立 layout。

### D4: 功能特色卡片

4 張卡片：
1. Automated Lending — Bot icon — 自動放貸，24/7 運行
2. Market Analysis — BarChart icon — 即時市場分析，智慧定價
3. Secure & Encrypted — Shield icon — AES-256 加密，API Key 安全存儲
4. Real-time Monitoring — Activity icon — 即時儀表板，掌握放貸狀態

### D5: Pricing 方案

| Plan | 月費 | 功能列表 |
|------|------|---------|
| Free | $0 | 基本儀表板、手動放貸 |
| Starter | $9.99 | 自動放貸（固定利率策略）、email 通知 |
| Pro | $29.99 | 進階策略（市場分析、多策略）、優先 API 配額 |
| Enterprise | $99.99 | 所有功能、專屬支援、自訂策略參數 |

## Risks / Trade-offs

- [SEO] → Server Component 渲染有利 SEO，但目前沒有 meta tags 最佳化（F15 處理）
- [i18n 內容] → 暫時用英文 key，messages 檔案加入對應翻譯
