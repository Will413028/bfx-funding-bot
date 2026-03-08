---
name: ui-premium-dark-theme
description: 當使用者要求建立或重構前端 UI 元件、儀表板或版面配置時觸發。強制使用「機構級量化放貸 SaaS」的暗黑質感，注重被動收益的穩定感與自動化運作的科技感。
---

# 🎨 量化放貸平台 UI 視覺設計規範 (Lending SaaS Premium Dark)

請嚴格遵守以下視覺限制，我們追求的是類似頂尖財富管理或機構級放貸面板的「冷靜、高科技、穩定感」：

## 🚫 絕對禁用的 AI 預設風格
1. **禁用預設灰色**：禁止使用 `bg-gray-100` ~ `bg-gray-900`。
2. **禁用基礎原色**：禁止使用 `text-blue-500`、`bg-red-500` 等高飽和預設色。
3. **禁用死板圓角與陰影**：不要用標準的 `rounded-md` 和 `shadow-md`。

## ✅ 放貸 SaaS 專屬設計手法 (Preferences)
1. **背景與層次 (Depth & Trust)**：
   - 網站底色永遠是純黑 `bg-black` 或深石板色 `bg-zinc-950`，營造極致的沉浸感與專業度。
   - 卡片背景使用極低透明度：`bg-white/[0.02]` 或 `bg-zinc-900/50`。
2. **邊框與光澤 (Glow & Borders)**：
   - 卡片必須帶有微光邊框：`border border-white/5`。
   - 頂部高光細節：可加入 `shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]` 營造高級儀器感。
3. **毛玻璃 (Glassmorphism)**：
   - 浮動元素 (Navbar, Dialog, Toast, Tooltip) 必須加上 `backdrop-blur-xl`。
4. **放貸平台專屬狀態色 (Lending Status Colors)**：
   - **產生收益中 (Active / Yielding)**：使用低明度螢光綠或青色 (如 `text-emerald-400`, `text-cyan-400`)。
   - **資金閒置 / 排隊掛單中 (Idle / Queued)**：使用具警示感但不刺眼的琥珀色 (如 `text-amber-500`)，提醒資金未被利用。
   - **系統錯誤 / 強制撤單 (Error / Canceled)**：使用冷紅色 (如 `text-rose-500`)。
   - **演算法運作中 / 動態複利觸發 (Bot Active / Compounding)**：點綴科技感的紫羅蘭色 (如 `text-violet-400`)，突顯機器人的智慧運作。