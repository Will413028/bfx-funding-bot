---
title: G2 live-vs-replay divergence 從 action parity 提升到 state parity（累加器走 rel-tol、有界 state 精確比對）
date: 2026-05-31
status: active
tags: [bfx-funding-bot, decision, g2, divergence, observability, verification]
related-commits:
  - "a2e8c33^..cec23b7"
---

# G2 divergence 從 action parity 提升到 state parity

## Context

`DivergenceReporter` 是 Phase 4.1 shadow infra 的 live-vs-replay 偵測器：live 是 daemon 啟動時 warmup、之後逐根 `observe()` 增量累積的策略；replay 是每 tick 用 `build_strategy_at_boundary` 從頭重建。兩條路徑真正獨立，但舊比對只看 `signal_direction`（POST/SKIP）與靜態屬性（config params、`candle.close`），`signal_score` 是 `±1.0` placeholder。

缺口：若 MR 的 `_ema` 或 RP 的 `_window` 靜默漂移（LOCF off-by-one、observe 順序差），但動作仍落在門檻同側，reporter 回報「無分歧」；潛伏漂移會在之後某根 boundary 以錯誤動作爆發——正是真錢階段。這是 online-offline parity（backtest-live parity／training-serving skew）的經典缺口：只比輸出、不比輸出背後的 state。

**約束**：

- `inherited` 不得改變任何策略的 `decide()`／`observe()` 數學與 `name`，backtest 與 live 方向位元不變：G3 Stage 2 同一紀律；現在仍成立，因 deploy gate 與 derive_cells 的數字依賴位元穩定。
- `inherited` 部署中的 canary 是 MR span=24、lookback=200：它決定累加器的收斂 noise floor。
- `external` M1/M2/M3 門檻要等 shadow window（約 2026-06-10）的真實分歧分布：本 slice 只做偵測能力，Component D（audit verdict＋門檻）延後，後來落在 [2026-08-30-g2-metrics-only-audit](2026-08-30-g2-metrics-only-audit.md)。

## Options Considered

**比對層級**
- **基準. State-level parity（online/offline feature parity 慣例）**：比對決定動作的 derived state／feature，而非只比預測；業界 training-serving skew 監控（例：[Google Rules of ML #37 "Measure Training/Serving Skew"](https://developers.google.com/machine-learning/guides/rules-of-ml)）
- **A. 維持 action-only**（direction＋靜態屬性）
- **B. state-level，全部欄位精確（byte）比對**
- **C（選用）state-level，依 state 性質分流**：有界 state 精確、無界累加器相對容忍
- **D. state-level，量化（rounding grid）後精確比對**

**signal_score**：維持 `±1.0` ／ 改為策略專屬的連續訊號強度 ／ 跨策略正規化分數

## Decision

- **D1 = 基準＋C**：MR 暴露 `ema_current`、`last_deviation`；RP 暴露 `last_threshold`、`window_filled`（read-only `@property`，只在 `decide()` 內 cache 已算出的值，回傳值不變），並以 **Decimal 精度**（不轉 float）放進 `_strategy_attributes`：RP `{percentile, last_close, last_threshold, window_filled}`、MR `{rate, threshold_sigma, ema_current, last_deviation}`。
- **D2 = C 的比對語意**：累加器衍生欄位（`ema_current`、`last_deviation`、MR `signal_score`）用相對容忍 `_REL_TOL=1e-4`；有界／離散欄位（RP state、config、`signal_direction`）精確比對；由 `_diff_fields` 決定是否分歧，而非 dataclass `==`。
- **D3 = 連續 score**：MR `signal_score = float(last_deviation)`；RP = `candle.close` 在當前 window 的 percentile rank（`100·count(w≤close)/len`）；state 不可用時 `0.0`。刻意不做跨策略正規化（只有 D 的彙總需要，YAGNI）。

## Rationale

- **D1**：只比動作會漏掉「state 漂了但還沒翻方向」這一整類 bug，而這類 bug 在真錢時才現形；暴露 state 的代價只是幾個 property 與 `decide()` 內一行 cache，不動決策數學。不選 A：正是要修的盲點。保留 Decimal 而非 float：float cast 會吞掉 sub-Decimal 漂移，等於自廢偵測。
- **D2**：對抗 review 發現 byte-equality 只對**有界** state 成立。RP window 是 `maxlen` deque，會精確遺忘舊資料，live 與 replay 可位元相等；MR `_ema` 是**無界**累加器，live 只在 warmup 播種一次，replay 每 tick 在 `ref_mts − lookback` 重新播種，seed 的指數尾巴 `(1−α)^lookback ≈ 5.6e-8`（span=24、lookback=200）在 Decimal 裡永不歸零。不選 B：MR 幾乎每個穩態 tick 都會誤報。`1e-4` 約在 noise floor 之上 140×、真實漂移（~1e-2）之下 100×。不選 D：rounding grid 有邊界假象，兩個只差 ~1e-8 的值跨在格線兩側會誤判分歧。代價：rel-tol 之內的真實漂移看不到，且 span=168 cell 在 lookback=200 內收斂不了（~9% floor），要更長 lookback 才能用容忍比對（未部署，不處理）。
- **D3**：連續值讓日後的 audit 能看分布距離而非二元一致；但 score 只是給人看的 float 診斷，精確偵測由 Decimal attributes 承擔。代價：score 跨策略不可比，彙總前須先正規化。

## Result

- `git log --oneline a2e8c33^..cec23b7`（spec、plan、G2 A/B/C 三個 feat、rel-tol review fix `5921841`、`_diff_fields` 註解）。
- 新紅測「方向相同但 `ema_current`／`last_threshold` 不同 → `strategy_attributes` 進 `diff_fields`」先紅後綠；既有 `test_no_divergence_when_inputs_match`、LOCF byte-equivalence、hypothesis `test_cp1_byte_equivalence_property` 保持綠。
- 現行行為已寫進 `backend/ARCHITECTURE.md` §4（`_REL_TOL=1e-4`、累加器相對容忍、其餘精確）。
- 延伸：AdaptivePeriod 的 B1 分支沿用同一原則——比 `ema_current`（rel-tol）、`window_filled` 與 `t1/t2/ratio_sigma`（精確），**不比**由它們推導的 `period_days` step function（見 [2026-06-04-adaptive-period-strategy-and-deploy-gating](2026-06-04-adaptive-period-strategy-and-deploy-gating.md)）。

## Invariants

- 新增的 state 欄位只供觀測，不得改變 `decide()` 回傳值、`observe()` 數學或策略 `name`。
- 累加器衍生欄位以 Decimal 進 divergence vector，不得先轉 float。
- 衍生自容忍比對輸入的 step function（如 period tier）不得直接放進精確比對欄位。

## Revocation Triggers

- canary 改用 span 更長（例如 168）或 lookback 更短的 cell → noise floor 升高，重新推導 `_REL_TOL` 或加長 lookback。
- 出現 rel-tol 之內卻導致方向翻轉的真實漂移案例 → 重評容忍值或改比較 replay 的 cold-start 對齊。
- 新增其他累加器型策略 → 依有界／無界分類決定比對語意，不可直接套精確比對。

## Lessons

### Rules

- **R1**：`Rule: parity 比對前先把每個 state 分成有界（可精確）與無界累加器（只能容忍），容忍值由 seed 尾巴 noise floor 與已知真實漂移量級夾出，而不是拍一個 epsilon。`（已收錄為通用規則：verification discriminating power，G2 EMA 累加器 rel-tol 比對案例。）

## Related

- 來源（SDD）：
  - spec：`2026-05-31-g2-state-level-divergence-design.md`（首次提交 `a2e8c33`；原文已不在 repo，本 ADR 即紀錄）
  - plan：`2026-05-31-g2-state-level-divergence.md`（首次提交 `256976f`；原文已不在 repo，本 ADR 即紀錄）
- Component D 的後續決策：[2026-08-30-g2-metrics-only-audit](2026-08-30-g2-metrics-only-audit.md)
- 上游 LOCF 與 G2 audit 定位：[2026-05-20-phase4.3-locf-staleness-budget](2026-05-20-phase4.3-locf-staleness-budget.md)
- 容忍帶後來在生產被驗證（漏根 ≈1.5e-2 遠超 1e-4，架構與 `_REL_TOL` 皆無問題）：[2026-07-27-candle-immutability-bitemporal](2026-07-27-candle-immutability-bitemporal.md)
