---
name: ui-financial-typography
description: 當涉及呈現金額、日利率、年化報酬 (APY)、放貸天數 (Period)、Order Book 牆或收益統計元件時觸發。
---

# 📊 放貸數據排版規範 (Lending Data Typography)

放貸儀表板的核心在於精確呈現「利率」、「天數」與「複利」。請套用以下規則：

1. **等寬數字 (Tabular Numerals) 絕對要求**：
   - 任何會跳動的數字（餘額、即時利率 FRR、預估利息），父層**必須**加上 `tabular-nums tracking-tight`，確保數字更新時畫面不抖動。
2. **利率 (Rate) 與 年化 (APY) 的層級區分**：
   - 當同時顯示日利率與年化報酬時，**APY 必須是主角**（字體較大、使用收益狀態色），日利率作為輔助資訊（字體較小、灰色）。
   - 符號弱化：百分比符號 `%` 必須比數字小或顏色更淡（例如：`<span className="text-zinc-500 text-sm">%</span>`）。
3. **放貸天數 (Period) 的標籤化 (Badging)**：
   - 放貸天數（如 2 Days, 30 Days）是關鍵策略指標。請用小型 Badge 呈現。
   - 短天數（2~7天）可用低調的背景色（如 `bg-zinc-800 text-zinc-300`）。
   - 長天數鎖利（如 30 Days）應有高亮或特殊的微光邊框，凸顯週末溢價或長線鎖倉策略的成功。
4. **小數點層級弱化**：
   - 顯示 $5,000.00 時，小數點後應弱化：`<span className="text-zinc-100">5,000</span><span className="text-zinc-500">.00</span>`。