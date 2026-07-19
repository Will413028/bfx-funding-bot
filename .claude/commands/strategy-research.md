# Strategy Research — 假設 → 5-gate 篩選 → 回測 → 報告（半自動策略研究管線）

**Input**：
- `/strategy-research <一句話假設>` — 單一假設走完整流程
- `/strategy-research batch` — brainstorm N 個假設 → **全部預登記後**批次跑（trials 先固定，FDR 才誠實）

方法論 SoT：`~/second-brain/wiki/tech/backtesting-methodology-best-practice.md`（§5-§10 + Decision Protocol）。
Registry SoT：`~/second-brain/wiki/projects/bfx-funding-bot/strategy-registry.md`。

## 三條紅線（不可協商）

1. **預登記後不得延伸**：假設空間（operationalization × horizon × params）在開跑前宣告死。跑完想「換個 horizon 再試」＝新假設 → 回步驟 1 重新登記、計入 trials（§6 跨輪 selection-effect）。
2. **絕不 auto-deploy / auto-promote**：任何 GO 候選停在報告 + registry `NOT-PROMOTED-PENDING-REVIEW`，promote 是 Will 的裁決（gate 制度見 registry 各列）。
3. **null result 一等結論**：全 KILL 是有效輸出，不放寬門檻硬造 champion。

## 步驟

### 1. Do-not-repeat 檢查

grep registry 全部三張表（策略/信號/執行層）比對新假設的信號族 + operationalization。命中 KILLED/REJECTED → 告知 user 已測過與依據，除非 operationalization 真的不同（要能說出差異在哪）否則 stop。命中 NOT-PROMOTED → 檢查 re-open 條件是否已滿足，未滿足 → stop 並回報條件現況。

### 2. 預登記（registry 寫入，開跑前）

registry 信號表加一行：`| <operationalization> | TESTING | <date> | mechanism: <一句話為何理論上抓得到訊號> |`。
batch 模式：**所有假設先全部登記完**才開始跑第一個。宣告內容必含：信號構造、horizon 集合、param 空間。commit registry（`daily:` prefix，second-brain repo）。

### 3. EDA 5-gate 漏斗

- 工具：`signal_eda.py`（新構造需加純函式 + unit tests，TDD；**不進 production `SIGNALS` registry**）+ `scripts/run_signal_eda.py`
- 5-gate：FDR-sig + regime 同號 + ≥3/4 cell + |median IC| ≥ 0.03 + **economic hurdle**（quintile spread 年化 ≥ `ECONOMIC_HURDLE_APR_PP`）
- 長 horizon（>30d）必跑 block-size 敏感度檢查（block ≳ horizon 列數；結果隨 block 放大消失＝偽顯著）
- VM 真資料執行模式（不碰 live bot）：`~/bfx-research` clone + 一次性容器
  `docker run --rm --label autoheal=false --network bfx_default --env-file ~/bfx-funding-bot/.env.runtime -v ~/bfx-research/backend_py/src:/app/src:ro -v ~/bfx-research/backend_py/scripts:/app/scripts:ro bfx-bot:local python -m scripts.run_signal_eda ...`
  （勿 mount 整個 /app——會蓋掉 baked venv；勿動 `~/bfx-funding-bot` 主 checkout / canary.env / 任何 live 容器）

### 4. KILL → 結案；GO → 回測

- KILL：registry verdict 更新 + 報告落 `docs/research/`，stop。
- GO：建策略 class（TDD，仿 `strategies/` ABC）→ 四 arm WFO/OOS（vs MR deployed params / AlwaysMarketRate / AlwaysFRR，仿 `run_frr_floor_backtest.py`）→ **模型外假設跑雙 bound**（fill 等；兩 bound 夾 0 → 結論=「等 live 量測」，不再調參）→ DSR 用 registry「DSR trials 計數」段的累計值（本次 sweep configs 數同步累加進去 + `DEFAULT_N_TRIALS`）。

### 5. 報告 + 收尾

- 報告 `docs/research/<date>-<slug>.md`（+.json），格式仿既有：四 arm 表、paired CI、honesty caveats、GO/KILL/NOT-PROMOTED 判定與理由
- registry verdict 落地（TESTING → 最終狀態；GO 候選標 `NOT-PROMOTED-PENDING-REVIEW` + 建議的 re-open/promote 條件）
- 呈給 Will：數字 + 候選疑慮清單（selection-effect？機械混淆？horizon 可行動性？cell 不對稱？）→ **等裁決，不自行 promote**
- gates：`uv run pytest -m "not integration"` 全綠 + mypy 不新增 + ruff 乾淨才 commit（Conventional Commits）

## Batch 模式補充

- 假設來源優先序：(1) registry re-open 條件已滿足的候選；(2) 07-06 profit review §3 外部訊號池（perp funding、清算瀑布——**需先 ingest 新資料源**，此為前置工另立 task）；(3) brainstorm 新假設（每個必附 mechanism）
- 批次全跑完才出總報告；批內 FDR 對全部檢定一起修

## 預期管理（寫給未來的自己）

主要輸出會是 KILL——live G3 timing alpha≈0、兩輪 8 operationalization 7 死是市場事實。本 command 的價值是把「便宜誠實地清空假設空間」從半天壓到半小時，不是 alpha 印鈔機。
