---
name: ui-bento-layout-motion
description: 當要求製作 SaaS 總覽頁面 (Dashboard)、策略設定面板或機器人狀態卡片時觸發。強制使用 Bento Box 網格與展現自動化質感的微互動。
---

# 🍱 放貸 SaaS 佈局與互動規範 (Bento Layout & Automation Motion)

## 佈局 (Bento Box Dashboard)
1. **打破傳統對稱**：放貸儀表板必須使用 CSS Grid 實作 Bento Box (便當盒) 佈局。
2. **區塊權重分配範例**：
   - **大區塊 (col-span-2 或 3)**：近期收益折線圖 (Yield Curve)、資金佈署分層分佈圖 (40/40/20 Tier Allocation)。
   - **中區塊**：活躍放貸單列表 (Active Offers)、巨單牆雷達 (Wall Radar)。
   - **小區塊 (單格)**：本日預估收益、當前市場波動度 (Volatility $\sigma$)、機器人心跳燈號。

## 微互動 (Micro-interactions & Bot Vibe)
請預設使用 `framer-motion` (若有安裝) 處理互動，營造「機器人正在背景努力工作」的生命力：
1. **狀態燈號呼吸燈 (Pulsing Indicator)**：
   - 當機器人處於「活躍波動期 (每 3 分鐘心跳)」時，狀態指示燈應有平滑的呼吸動畫 (`animate-pulse` 或 Framer Motion 的 `repeat: Infinity`)。
2. **數字遞增動畫 (Number Ticking)**：
   - 當總收益增加時，數字不要瞬間變換，應該有快速跑動向上滾動的視覺效果 (Count up)。
3. **按鈕物理回饋 (Spring)**：
   - 啟動/停止機器人、儲存策略參數等操作，按鈕必須帶有 `active:scale-[0.98]` 創造按壓的物理 Q 彈感。
4. **列表交錯載入 (Staggered Load)**：
   - 當更新 Order Book 巨單牆資料時，清單項目應以交錯 (Staggered) 的方式由下往上平滑浮現。