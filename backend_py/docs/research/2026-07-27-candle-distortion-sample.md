# Candle 失真觀測樣本（L4 敏感度分析的輸入）

**這份是唯一倖存的原始證據。** 來源 container log 已於 2026-07-26 部署（`docker compose up -d` recreate `bfx-bot`）時消失，且 `funding_candles` 在 migration 前無任何時間戳，**無法重新取得**。任何要重跑的分析都必須用這份。

## 取得方式（已不可重現）

2026-07-27 從 `bfx-bot` 容器 log 抽出 7 天內 `divergence_detected` 行的 `lend_decision(mts, rate)`——即 live 當下 `observe()` 到的 close——再對照 `funding_candles` 同 `mts` 的現值。

```
docker logs --since 168h bfx-bot | grep "divergence_detected cell=mean_reversion:fUSD_a30" | ...
```

## 母體

| 項目 | 值 |
|---|---|
| 比對筆數 | 132 |
| 不符筆數 | **23（17.4%）** |
| 樣本 series | `fUSD` / `a30` / `1h` 單一組合 |
| 窗口 | 2026-07-19 ~ 2026-07-26 |

## 偏差樣本（n=20）

`pct = (db_final − live_observed) / live_observed × 100`；正值代表最終值高於 live 當時看到的。
原查詢帶 `LIMIT 20`，故 23 筆中僅存 20 筆。

```
-0.068, -33.924, -1.024, 13.682, -0.191,
-22.241,  0.111, -4.547, -35.330,  2.602,
  0.508,  0.862,  5.532,  0.032,  -0.673,
  0.481,  0.021,  3.671, -9.792,   1.443
```

反向換算（backtest 持有 `final`，要模擬 live 看到的值）：

```
live_observed = db_final / (1 + pct/100)
```

## 已知偏誤（分析時必須聲明）

1. **單一 series**：只有 `fUSD_a30`，未涵蓋 `fUST` 或其他 `period_agg`；不同流動性的 series 失真率可能不同。
2. **截斷樣本**：23 筆取到 20 筆，遺失的 3 筆非隨機（依 `mts DESC` 排序，缺的是最舊的 3 筆）。
3. **窗口短**：7 天，未跨越不同波動環境。
4. **僅含被偵測到的失真**：來源是 `divergence_detected` 行，若某根 candle 失真但未觸發 divergence 就不在樣本內 → **真實失真率可能高於 17.4%**。
5. **不含 2026-07-26 之後**：定稿機制上線後失真機制已改變，樣本描述的是修復前的世界（這正是要拿來測 backtest 的那個世界）。

## 用途

L4 敏感度分析：對 backtest 的 candle 序列以 17.4% 機率注入取自上述經驗分布的擾動，重跑 WFO/OOS，測「bot-vs-idle 年化 7.7%/10.3%、每月皆正、deflated-Sharpe 1.0」等結論的 robustness。

**這個分析只能回答「結論脆不脆弱」，不能給出真實的 live 可實現績效**——歷史 point-in-time 值已永久遺失。見 ADR `wiki/projects/bfx-funding-bot/decisions/2026-07-27-candle-immutability-bitemporal.md` 的 D2。

## ⚠️ 第一版 L4 結果（2026-07-26 20:03，N=500）不可採信

`~/bfx/reports/2026-07-27-l4-distortion-sensitivity.md` 那張表顯示「500 次擾動下每月皆正全數維持 100%、月報酬中位數幾乎不動」，**看起來像結論很穩健，但那是設計造成的假象**。

**模型缺陷（比上面五項偏誤嚴重——前五項影響精度，這項影響結論方向）**：backtest 裡 `candle.close` 同時扮演兩個角色——

1. **策略的觀察值**：決定 POST/SKIP 與掛單 rate
2. **市場的真實利率**：決定成交與收益

`perturb_candles` 把整條序列的 close 一起改掉，於是策略看到失真價格、收益也照失真價格計算。這模擬的是「市場利率真的變了」，而真實情境是「**bot 看到失真價格，但市場利率是最終值**」。兩個角色一起偏移，誤差在決策端與收益端互相抵消——**這個實驗幾乎不可能失敗，因此它的通過不構成證據**（同 `wiki/tech/verification-discriminating-power` 的核心命題）。

**要真正回答，需要讓評估路徑吃兩條序列**：`strategy.observe()` 用失真序列，fill / PnL 用真實序列。`evaluate_oos_windows(candles, ...)` 目前只收單一序列，得先改 backtest 引擎才能分離。在那之前，**WFO/OOS 結論維持「未確認」**，不得以第一版 L4 當作解除依據。
