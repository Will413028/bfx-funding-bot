# Strategy Journal

> 放貸策略實驗紀錄。記錄每次重要的策略測試、參數調整、和學到的結論。
>
> 量化結果未來會存在 DB 的 performance tracking table，這裡記錄**為什麼**做這個實驗、學到了什麼、下一步打算做什麼。

---

## 格式範例

### YYYY-MM-DD: 實驗標題

**假設**: 為什麼要做這個實驗

**測試內容**:
- 調整的模組/參數（e.g. pricing MDC premium, floor regime multiplier, allocation tier）
- 幣種 + 觀察區間
- 市場狀況（regime, FRR 水位, 大盤走勢）

**結果**:
- 關鍵指標：fill rate, 平均日利率, APY, 平均鎖定天數
- 定性觀察：排隊速度、offer 被吃的模式、與 FRR 的偏離

**結論**: 學到了什麼，是否採用此調整

**下一步**: 接下來要試什麼

---

## 關注指標

| 指標 | 說明 | 目標方向 |
|------|------|----------|
| Fill Rate | offer 被成交的比例 | 越高越好（>80%） |
| Avg Daily Rate | 平均日利率 | 在 fill rate 可接受下越高越好 |
| APY | 年化報酬率 | 綜合衡量 |
| Avg Lock Days | 平均鎖定天數 | 視流動性需求而定 |
| Zombie Rate | 超過 TTL 未成交被取消的比例 | 越低越好（<15%） |
| Queue Depth | offer 在 order book 的排隊位置 | 前 50% |
| Capital Utilization | 資金實際放貸比例 | >90% |

## 可調整的策略模組

| 模組 | 關鍵參數 | 影響 |
|------|----------|------|
| Pricing | MDC premium (±50%/±30%), regime bonus | 最終報價利率 |
| Floor | FRR relative (80%), regime multiplier | 最低可接受利率 |
| Queue Position | depth discount (-2%/-5%) | 排隊過深時降價幅度 |
| Market Impact | threshold (10%/25%), premium (+3%/+5%) | 大額 offer 的溢價 |
| Weekend | Fri/Sat/Sun premium (+2%/+5%/+3%) | 週末溢價 |
| Calendar | month-end (+2-5%), quarterly (+3-10%) | 特殊日期溢價 |
| Lockup Cost | base 0.5bps/day, vol amplifier | 長天期的補償 |
| Allocation | tier split (50/30/20), regime caps | 資金分配層數 |
| Period | regime ratio (75%/50%/25%), vol discount | 放貸天數選擇 |
| Splitting | depth threshold (5%), max 5 orders | 大單拆分 |
| Noise | rate ±1%, amount ±2% | 避免被偵測 |

---

## Log

### 2026-03-21: 策略引擎全面 Review — 上線前收益優化

**背景**: 引擎 Phase D（13 策略模組）+ Phase E（執行層）全部完成，上線前進行完整 review。

**發現的問題**:

#### P0 — 策略 Pipeline 未接線（最嚴重）

`factory.go` 只實例化 `PricingStrategy`，其餘 12 個策略模組**寫好但未使用**。等於目前引擎只用了 1/13 的策略能力。缺少 floor 保護、allocation 分層、splitting、queue 調整、weekend/calendar 溢價等所有功能。

#### P1 — 參數調校（6 項常數修正）

| 參數 | 目前值 | 問題 | 建議值 |
|------|--------|------|--------|
| `frrFloorRatio` | 0.80 | 接受 FRR 打 8 折，賤賣資金 | 0.90–0.95 |
| `maxPremiumDown` | 0.30 | Backwardation 降價 30% 太慷慨，race to bottom | 0.15–0.20 |
| `regimeContangoPeriodRatio` | 0.75 | Contango 是高利率窗口，應更積極鎖長期 | 0.85–0.90 |
| `rateNoiseRange` | 0.01 (±1%) | 放貸 spread 很薄，1% noise 影響排隊位置 | 0.003–0.005 |
| Allocation aggressive mul | 1.25× | 比 base 高 25% 的 offer 不太可能成交 | 1.10–1.15 |
| Queue discount | 兩段式跳躍 | 在 threshold 附近震盪 | 改用線性/sigmoid 曲線 |

#### P2 — 缺失的收益關鍵邏輯（6 項新功能）

1. **閒置資金急迫度**: 資金閒置越久應越願意降價，目前不追蹤閒置時間。死資金的 APY = 0%，任何正利率都比 0% 好。建議加入 `urgencyDiscount = min(idleMinutes / 120, 0.15)`

2. **FRR 趨勢追蹤**: 只看 spot FRR 太短視。FRR 連續上升 4 小時 vs 剛剛 spike 是完全不同的信號。建議加 EMA(30min) vs EMA(4hr) 趨勢判斷

3. **歷史 Fill Rate 學習**: 目前所有 premium/discount 都是固定常數。應追蹤每個 rate bucket 的實際 fill rate 和 time-to-fill，用數據驅動定價取代猜測

4. **Auto-Renew 重新定價**: Credit 到期時直接重新提交相同參數，沒有根據當前市場重新定價

5. **智慧貼牆策略**: 目前靠近 wall 只降 1%，應改為 price just below wall 搶先被吃。Wall 提供的是定價資訊而非威脅

6. **Order Book Gap Detection**: 在 book 的空隙報價幾乎肯定成交（無競爭者），目前沒有利用此機會

**結論**:
- P0（pipeline 接線）是最大瓶頸，解鎖後預期 APY +30-50%
- P1 的 6 項常數修正可快速實施
- P2 的新功能中，「閒置資金急迫度」和「FRR 趨勢追蹤」對 capital utilization 和擇時能力提升最大

**下一步**: 依 P0 → P1 → P2 順序實施，詳見 ROADMAP Phase G+ 新增項目

---

### 2026-03-21: 策略規範設計層面 Review — 根本性收益盲點

**背景**: 完成實作層面 review 後，以虛擬貨幣放貸專家角度重新審視 `strategy_specification.md` 的策略設計本身。

**發現的設計問題**:

#### S1 — 定價模型根本性問題（3 項）

1. **定價應基於 bestAsk 偏移，而非 FRR 乘數**
   - Bitfinex offer 按利率排序 + FIFO 匹配。比 bestAsk 低 1 tick 幾乎肯定成交，高 2% 可能永遠排不到
   - 目前 §2.6 用 FRR × MDC premium 定價，但真正的競爭力取決於相對 bestAsk 的位置
   - 建議：`targetRate = bestAsk - tickOffset(MDC, regime)`

2. **Auto-Renew 應預設開啟而非關閉**
   - §7.2 說「永遠關閉」，但 auto-renew 是免費保險。引擎宕機時資金空轉 APY=0%
   - 正確做法：永遠開啟作為安全網，引擎正常時主動在到期前取消並重新走 pricing pipeline

3. **Noise 策略在放貸市場是反效果**
   - 放貸不是零和博弈，沒有前跑風險。隨機擾動只是在燒錢
   - 心理價位避讓（§4.7）有用，但 §4.14 的隨機 noise 應移除或限制為只有 + 方向

#### S2 — 信號框架的收益盲點（3 項）

4. **缺少 RatePercentile 信號 — 最直接有用的單一指標**
   - 放貸利率是均值回歸的。MDC 試圖預測方向（交易思維），但放貸真正需要的是「當前利率在歷史分佈中排第幾」
   - 利率 > P75 → 積極放貸 + 鎖長期。利率 < P25 → 短期或等待
   - 這個單一信號可能比 6 個 MDC 信號組合更能決定「現在該不該放」

5. **清算瀑布回應太粗糙 — 錯過分階段操作**
   - 瀑布有早/中/後三階段，每階段最佳策略不同
   - 早期（0-30min）：短期 2d + 最高利率，捕捉瞬間暴利
   - 中期（30min-2hr）：中期 7-14d，鎖住確認的高利率
   - 後期（2hr+）：停止新增，利率已在下降

6. **Weekend Premium 嚴重低估**
   - 設計 +2~5%，但實際週末利率比平日高 20-50%（正常市場）甚至 3-5× （大行情）
   - 應用歷史 weekend/weekday ratio 動態計算：`premium = rolling4wk_weekend_rate / rolling4wk_weekday_rate`

#### S3 — 策略架構缺失（4 項）

7. **所有幣種共用同一套參數**
   - fUSD（低波動、高流動性）和 fETH（高波動、事件驅動）的最佳策略完全不同
   - MDC 權重、regime 閾值、premium 映射應至少分 stablecoin / crypto 兩組

8. **缺少信心度 × 部署比例**
   - MDC 只影響 rate 和 period，不影響部署多少資金
   - 信號一致（高 confidence）→ 部署 100%。信號矛盾 → 只部署 50-70%，保留彈藥

9. **缺少滾動放貸 / 到期梯隊管理**
   - 應主動維持 1/3 短期 + 1/3 中期 + 1/3 長期的到期結構
   - 短期到期時根據 RatePercentile 決定轉長期（>P75）還是維持短期（<P25）

10. **P(higher_rate) 可用均值回歸模型精確計算**
    - §4.8 的 EV_wait 需要 P(higher_rate)，spec 說「歷史統計」但沒公式
    - 利率均值回歸：`P(higher) = Φ((EMA_7d - current) / σ√t)`，直接可算

**結論**:
- S1 的三項（bestAsk 定價、auto-renew 反轉、noise 移除）能立即提升收益，不需要新的數據或基礎設施
- S2 的 RatePercentile 信號是對 MDC 框架最有價值的補充
- S3 的 per-currency 參數和滾動放貸是中長期架構改進

**下一步**: 更新 strategy_specification.md §11 + ROADMAP Phase G 新增 S1-S3 項目

---

### 2026-03-21: 市場微觀結構 Review — 隱形收益殺手

**背景**: 完成實作 review 和設計 review 後，從 Bitfinex 放貸市場的微觀結構角度進行第三輪審查。

**發現的問題**:

#### M1 — 直接影響到手收益（3 項）

1. **Bitfinex 15% 手續費完全被忽略**
   - 整份 spec 沒有出現「手續費」。有效日利率只有名義的 85%
   - 影響：floor 計算、period 最佳化、複利效率比較全部有偏差
   - 尤其是短期 vs 長期的比較：短期有更多複利機會但也有更多「成交空檔」損失。考慮 15% 手續費後，穩定利率下長期單略優

2. **「成交空檔」未追蹤 — 隱形 capital downtime**
   - Credit 到期到新 offer 成交的間隔（估計平均 20min）
   - 2d period = 月 15 次到期 = 5hr 空轉 = 0.7% downtime
   - 30d period = 月 1 次到期 = 0.3hr 空轉 = 0.05% downtime
   - 解法：到期前預排程下一筆 offer + 追蹤 `avgGapMinutes` 持續優化

3. **缺少 Proactive Offer Refresh — 白白放棄 FIFO 優勢**
   - Bitfinex 同利率 FIFO 匹配。排太深的 offer 等很久
   - 目前只在「stale」時才撤。應該在排隊超過 30% 深度時主動撤單重掛到 bestAsk - 1tick
   - 跟 §7.1 的隊首保留互補：已在隊首 → 保留；排太後 → 主動搶隊首

#### M2 — 風險定價缺失（3 項）

4. **提前歸還的逆向選擇未量化**
   - 借方在利率下跌時還（對你不利），利率上漲時不還（對你不利）
   - 這是負凸性 / callable bond 風險。長期單需要提前歸還風險溢價
   - 公式：`earlyReturnPremium = historicalEarlyReturnRate × (period / 30) × 0.05`

5. **非清算性的利率 Spike 沒有偵測**
   - §3.5 只偵測清算瀑布。但大戶開倉、套利需求、流動性事件也會造成 spike
   - 需要純利率 spike 偵測：`if currentRate > EMA_1h × 2.0 → spike = true`
   - 反應比等 MDC 慢慢升上去快得多

6. **FRR 可被操縱 — 不應盲目跟隨**
   - 大戶可用 wash lending 拉低 FRR。你的 floor（FRR × 0.90）跟著被拉低
   - 解法：`effectiveFRR = max(FRR, bookMidRate × 0.9)`，同時看 order book

#### M3 — 架構級改進（2 項）

7. **Temporal Laddering — 時間分散比利率分散更有效**
   - 不是同時掛 3 筆不同利率 offer，而是跨 3 個心跳分批部署
   - 類似 DCA：第一批立即成交，後續批次可能捕捉到更好利率（或至少不全部踩在同一個 snapshot）

8. **FRR 反饋迴路 — 大規模時的正反饋風險**
   - 你的 credits 影響 FRR → FRR 影響你的定價 → 你的定價影響 credits
   - 管理資金佔市場 > 5% 時，降低 FRR 在定價中的權重，避免正反饋失控

**結論**:
- M1 的三項（手續費、gap cost、offer refresh）直接影響每天的到手收益，應最優先
- M2 的提前歸還溢價修正了長期單定價的根本性偏差
- M3 在管理規模擴大時變得重要

**下一步**: 更新 strategy_specification.md §13 + ROADMAP Phase G 新增 M1-M3 項目
