---
title: 後端 Go → Python 單一語言重寫（quick re-do＋dated abort gates）與 Day-3 stack：FastAPI、SQLAlchemy 2.0＋Alembic、手寫 Bitfinex client、vertical-slice 版面
date: 2026-05-09
status: active
tags: [bfx-funding-bot, decision, architecture, python, spec-compression]
related-commits:
  - "2dc59ce^..c1903c3"
  - f6e7e69
  - b9c1f37
---

# 後端 Go → Python 重寫與 Day-3 stack

## Context

2026-05-09 評估歷史利率資料源時（唯一路徑是官方 public REST），Will 決定把約 14k 行的 Go 後端（Gin＋fx＋sqlc＋Atlas）整個改寫成 Python。05-10 早上用五個問題重新挑戰這個決定，撤回了幾條理由，但方向不變；同一天定下 Day-3 checkpoint 需要的三個 stack 選擇和程式碼版面，並在當天通過 Checkpoint 1 與 2。Go 時代設計在語意層被反轉的部分另見 [2026-09-23-go-era-openspec-design-disposition](2026-09-23-go-era-openspec-design-disposition.md)。本 ADR 記錄的是語言、遷移路徑與 stack 的選擇。

**約束**：

- `external` Bitfinex funding endpoint 限流 30 req/min，放貸決策不吃延遲；語言執行效能不是瓶頸。
- `external` solo 開發者；兩套語言的維運面是實際負擔。
- `inherited` Neon 上已有 Atlas 套用過的 schema（`funding_candles` 等）：當時仍是資料真相；現在 DB 已改自托，但 Alembic chain 是唯一的 migration 歷史，仍成立。
- `inherited` Stage 1.5 backtest go/no-go 還沒過、沒有 live 用戶，前端只有自己在用：決策當時成立，所以凍結前端沒有成本；現在已不成立（05-26 起 live），但重寫已完成，不影響本決策。
- `inherited` Go 版的 Bitfinex client 是手寫的，因為 Go SDK 薄到沒有附加價值：Python 版重新驗證後仍成立。

## Options Considered

**語言與遷移路徑**

- **基準. Strangler Fig 漸進替換**（[Fowler, StranglerFigApplication](https://martinfowler.com/bliki/StranglerFigApplication.html)）：先在新語言做一塊，驗證後再逐步搬。
- **A. 維持 Go**，研究也用 Go 做。
- **B. Split brain**：Python 做研究，Go 做 production，驗證過的策略再 port 回 Go（5/9 最初的建議）。
- **C. 單一 Python，big-bang 改寫（選用）**：5/10 改框為 quick re-do（P50 1 週／P90 2 週），加上 dated abort gates。
- **D. Hybrid**：Python 改寫但只做最小可行版本，lending engine 延後。

**Stack（Day-3 gating 的三項）**

- Web：**FastAPI（選用）**／Litestar／先不做 web（純 CLI，當時 Claude 的建議）。
- DB：**SQLAlchemy 2.0 async＋typed `Mapped[]`（選用）**／psycopg3 raw＋自寫 typed wrapper（Claude 的建議，最接近 sqlc）／SQLModel。
- Migration：**Alembic（選用）**／保留 Atlas；baseline 方式：**從現有 DB autogenerate、確認 diff 為空再 stamp（選用）**／手寫空的 revision 0。
- Bitfinex client：**`httpx`＋`websockets` 手寫（選用）**／官方 `bitfinex-api-py` v4.0.0 直接用或包一層／`ccxt`。

**程式碼版面**：分層（`infra/`、`domain/`、`db/`、`repositories/`）／**vertical slice（選用）**：`core/`＋`modules/<feature>/`（schemas、tables、repository、service）＋`external/<vendor>/`。

## Decision

- **D1（parent spec §Decision＋Fresh-mind Review）**：單一 Python stack，選 C 而非 A／B。前端 Next.js 不變。
- **D2（§Path、§Abort criteria）**：big-bang，不採 Strangler Fig 或 Hybrid；以 `backend_py/` 與凍結的 Go `backend/` 並存於同一 repo，不另開新 repo。原本「2–3 週後 commitment 變硬」的說法撤回，改成硬性關卡：Day-3 端到端 candle round-trip、Week-1 回測骨架產出月報酬與 drawdown、Week-2 底線，任何一關失敗就回到 Go，不延長。
- **D3（stack Q1）**：FastAPI 從第一個 commit 就用。
- **D4（stack Q2）**：SQLAlchemy 2.0 async＋Alembic，採 baseline-from-DB；autogenerate 如果不是空的，就修 model，不套用那份 diff（schema 才是真相）。Atlas migration 歸檔。
- **D5（stack Q3）**：手寫 client，上層只看得到 Pydantic domain model；限流用 `aiolimiter`，金額欄位一律 `Decimal`。
- **D6（plan 9ab1c08／29db864）**：vertical slice。規則：`core/` 不 import `modules/`、`external/`；`external/` 不 import `modules/`；`modules/` 之間的依賴必須無環。
- **D7（stack §Frontend、§Open questions）**：改寫期間前端一起凍結。其餘 12 個子決策（多用戶併發模型、feature flag、observability、前端契約等）延到 Checkpoint 2 之後再決定。

## Rationale

- **D1**：5/10 撤回了三條理由：「研究速度快 5–20 倍」是憑感覺的估計（策略數學只是 rate×period×notional−fee，不是 quant ML）；「策略只寫一次、沒有 port gap」在策略數為 0 時是想像出來的痛點；「SDK 讓手寫變多餘」也被 Q3 推翻。留下來的是比較弱但真實的理由：開發者對 Python 資料工具的熟悉度、單一維運面、Python 生態讓手寫 client 很便宜。不選 B，是因為 solo 維護兩套語言、外加 port 回 Go 的成本，比 Go 的型別保證更貴。不選 A，是因為研究工作流在 Go 沒有對應工具。代價：放棄 Go 的編譯期保證，改由 Pydantic strict＋mypy strict 補大約八成。
- **D2**：Strangler Fig 要把工作做兩次，Hybrid 的「最小」邊界又說不清楚。改框為 1–2 週之後，big-bang 最大的風險（沒有退路）被 dated abort gates 限住，而不是靠「再給一週」。
- **D3**：Pydantic 原生、有 OpenAPI，也避免日後在時間壓力下再選一次框架。不選 Litestar：它的效能優勢在 30 req/min 下沒有意義，生態也比較小。代價：從一週預算裡拿出約半天做 scaffold。
- **D4**：typed `Mapped[]`＋mypy 是 Python 裡最接近 sqlc 的做法；回測的時間序列聚合最後也會把 raw SQL 推回 ORM。不選 SQLModel：它的抽象一碰到非簡單查詢就漏回 SQLAlchemy，而且維護節奏慢。改用 Alembic 而非 Atlas：雙語言的局面結束後，語言中立不再有價值，autogenerate 又直接讀 model。baseline-from-DB 相較空的 revision 0，第一天就逼出 model 與 schema 的偏差。
- **D5**：驗證過 SDK v4.0.0：funding endpoint 齊全，但 REST 是同步的 `requests`、沒有限流、repo 裡沒有 `tests/`，而且 v4.0 才剛重寫。它本質上只是包一層 `requests.get`。手寫則從第一行就是 async，重試、限流、錯誤對應都在自己手上，這在真錢路徑上是必要的。不選 `ccxt`：funding endpoint 只以 raw method 暴露，沒有統一抽象。代價：Bitfinex API 的欄位與分頁變動要自己追。
- **D6**：新功能以新增 `modules/<feature>/` 的方式加進來，不必改既有模組。代價是版面規則只靠慣例維持（見 Result O2）。
- **D7**：沒有用戶時，凍結前端的成本是零，也少一個跨層變動的來源。Checkpoint 2 之前先把併發模型想清楚，萬一 abort 就是白做。

## Result

- `git log --oneline 2dc59ce^..c1903c3` 是 scaffold 到 Checkpoint 2 的完整區間，全部在 2026-05-10 完成，遠早於 P50。
- Checkpoint 1 PASS：`backfill_candles.py` 把 24 根 fUST 1h p2 candle 寫入再讀回，全部在 `Decimal("1e-15")` 容差內（web UI 人工比對當時記為 pending）。Alembic baseline 的 autogenerate diff 為空（`f6e7e69`）。
- Checkpoint 2 PASS：`run_backtest.py` 對 719 根 candle 跑 AlwaysFRR（2 天期）：15 筆、0.4028%/月、drawdown 0%。這是沒有任何摩擦的 toy 數字，後續由 [2026-05-10-backtest-friction-baseline](2026-05-10-backtest-friction-baseline.md) 取代。摩擦紀錄：`session.bind` 方言偵測脆弱、float 轉 Decimal 要經過 `Decimal(str(x))`、手寫 client 第一次打真 API 就成功。
- Go `backend/` 於 `b9c1f37`（05-29）刪除，`backend_py/` 於 `455fc29`（09-23）改名為 `backend/`。
- **O1**：部分理由沒有實現。Polars 與 Jupyter 從未引入（研究用 pandas＋numpy 腳本，repo 裡 0 個 `.ipynb`），前端也沒有從 OpenAPI codegen。真正撐住決策的是單一 stack 與便宜的手寫 client。
- **O2**：D6 的 import 規則已經被侵蝕。origin/main 上有 7 個 `external/bitfinex/*.py`（`rest`、`auth_rest`、`auth_ws`、`live_executor` 等）import `modules/`，`core/writer_lock.py` 也 import 了 `modules/`。

## Followup

- (決策) D6 的 import 方向規則要恢復（例如加 import-linter 守門、把共用型別下移到 `core/`），還是改寫成現況（`external/` 可以依賴 domain 型別）；二選一後同步寫進 `backend/ARCHITECTURE.md` §2。

## Revocation Triggers

- 出現對延遲敏感的策略（秒級以下的 book 反應）→ 重評 D1 的「效能不是瓶頸」。
- 手寫 client 的 venue 欄位誤讀反覆發生（例如 `3689adb` 修正 funding credit 欄位的讀取位置），且官方 SDK 已提供 async 與測試 → 重評 D5。
- SaaS 多租戶開始實作 → D7 延後的併發模型要正式決策（前提見 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)）。

## Related

- 來源（Provenance）：
  - `2026-05-09-backend-rewrite-to-python-decision.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-python-rewrite-day3-stack-decisions.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-python-rewrite-day1-to-week1.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-10-checkpoint-1-result.md`、`2026-05-10-checkpoint-2-result.md`（原文已不在 repo，本 ADR 即紀錄）
- 相關 ADR：[2026-09-23-go-era-openspec-design-disposition](2026-09-23-go-era-openspec-design-disposition.md)（Go 時代語意的處置；D6 已記錄 SDK 重新評估）、[2026-05-10-backtest-friction-baseline](2026-05-10-backtest-friction-baseline.md)、[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)（Alembic 是唯一 migration 工具）。
