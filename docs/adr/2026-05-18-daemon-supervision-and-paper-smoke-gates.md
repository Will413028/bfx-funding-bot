---
title: Daemon 監督採 TaskGroup + 子任務 retry + 平台重啟，paper smoke gate 以結果計數與逐 cell recency 判定
date: 2026-05-18
status: active
tags: [bfx-funding-bot, decision, phase-4, daemon, supervision, heartbeat, smoke-test]
related-commits:
  - "eab4b96^..0f3dbe3"
  - "fbdd16c^..83ae38f"
  - 3092d02
  - d7d2a23
---

# Daemon 監督採 TaskGroup + 子任務 retry + 平台重啟，paper smoke gate 以結果計數與逐 cell recency 判定

## Context

Phase 4.1 shadow run（2026-05-18）起跑 14 分鐘 DB 連線被 idle 切斷、4 小時後 daemon 變 zombie：process 活著、health check 回 OK，candle pipeline 早已斷。4.2.0 修三層 root cause（連線、監督、健康判定），要求連跑 ≥48h 才解鎖真錢開發。2026-05-21 又發現 4.1 G1 smoke 的 C1 continuity（「每 5 分鐘桶至少 1 個 signal、相鄰間隔 <5min」）在 11 個 cell 全 `timeframe: 1h` 下由鴿籠原理**必然失敗**——是 spec bug 不是實作 bug。

**約束**：

- `external` Neon serverless 15 分鐘 idle 切連線；Koyeb 單容器 PaaS、SIGTERM→SIGKILL grace 60s，沒有多 process 預算。
- `external` Bitfinex public channel `hb` 每 15–30s；1h cell 每小時只 emit 1 個 signal。
- `inherited` 4.1 的 `asyncio.gather` + try/except 協調器（zombie 來源）；仍成立的理由：當時唯一 runtime，要在其上改而非重寫整個 daemon。
- `inherited` 系統尚未上線，不需顧 backward compatibility（G1-C1 spec 明言），可直接改成業界形狀。

## Options Considered

**監督模型**（基準 = Python structured concurrency + 交給 process supervisor 重啟：PEP 654 TaskGroup + 12-factor IX Disposability）：

- **A. `asyncio.gather` + try/except**（4.1 現狀）：子任務死了沒人知道——zombie 直接來源
- **B. 分層：TaskGroup + 子任務內 `tenacity` retry transient + fatal 冒泡由平台重啟**（選用，即基準加一層 in-task retry）
- **C. 自寫 OTP 式 per-task supervisor**（aiomisc / Erlang one_for_one）
- **D. 純 let-it-crash**：不做 in-task retry
- **E. 多 process + watchdog**（HFT 做法）

**健康判定**：k8s 式 process self-report ／ **registry + 每子任務 staleness threshold（選用，Prometheus `last_update_time` 慣例）** ／ pub-sub heartbeat event ／ inspect asyncio Task 狀態。

**Paper smoke 計數來源**：daemon 內部 counter ／ **端到端查 event sink（選用）**。

**G1 C1 continuity 判準**：5 分鐘桶 + 相鄰間隔（原定義）／ **逐 cell max-gap + recency，容忍 1.5× 預期週期（選用，Cronitor／Healthchecks.io grace、Prometheus `absent_over_time`）**／再加 CV 規律性或間隔下限。

**Smoke 觸發**：人工事後跑 script ／ subprocess 呼叫 script ／ **daemon 結束後自跑、以 module 形式 import（選用）**。

## Decision

- **D1 = 分 spec**：4.2.0 daemon stabilization 與 4.2 real-money 分兩份 spec，而非一份 mega-spec 或三段切（safety 與 venue 接線原子，切開是假分割）。
- **D2 = 監督選 B**：TaskGroup one-for-all；每子任務只把最內層 I/O 包 `tenacity`（exp backoff 1→30s、5 次）；超過上限或分類為 fatal 就 raise → TaskGroup 取消全部 → 非零退出 → 平台重啟。daemon 不自帶 restart 計數。
- **D3 = Transient／Fatal 分類表**：DB idle/closed、WS 1000/1001/1006/1011、429/5xx/timeout、DNS/TLS → transient；DB auth／db 不存在、Bitfinex WS 4400/4401/4404、HTTP 401/403、`MemoryError` → fatal。
- **D4 = heartbeat registry**：子任務每完成一個 progress unit 寫 `last_active_ts`；超過 threshold 發 degraded，>3× threshold 升 fatal。retry 期間不更新（沒有 progress 就該被看見）。
- **D5 = DB 連線兩層**：engine `pool_pre_ping=True` + `pool_recycle=600`，加 daemon 級每 5 分鐘 `SELECT 1`（= recycle/2）。alembic 走 sync psycopg、runtime 走 asyncpg，settings 各給一個 URL accessor，不用 async alembic wrapper。
- **D6 = 關機**：timeout 交給平台 grace（12-factor），不在 app 裡包 `wait_for(55s)`——spec 原圖有，plan 階段刻意偏離。
- **D7 = 出場門檻**：Gate 1 = 連跑 ≥6h + 手動 chaos（Neon suspend 30s、封鎖 event sink 60s）；Gate 2 = ≥48h、≥100 signal、無 >5 分鐘持續 degraded。long-soak + chaos 並用。
- **D8 = 測試**：整合測試用 testcontainers Postgres + `pg_terminate_backend`；chaos 先手動 runbook（重複 ≥3 次才自動化）；smoke 只測 happy path，exit 1/2 邏輯交 unit test。
- **D9 = paper smoke**：以 event sink 端到端查 signal 數判定（≥ active cell 數 → 0；timeout 90 分鐘內 0 → 1；部分 → 2），不用 daemon 內部 counter。
- **D10 = G1 C1 重定義**：逐 cell C1a max-gap ≤ 1.5×週期、C1b `now − last_signal` ≤ 1.5×週期；0 signal 直接 fail，1 signal 跳過 C1a；LOCF cell 不特例；`window_end = now`（嚴格）。
- **D11 = self-smoke**：paper + 有 duration 時，daemon `_run()` finally 後睡 30s 跑 G1，容器 exit code = G1 結果；smoke 邏輯搬進 `src/.../smoke/`，script 變 thin wrapper；smoke 失敗引發的平台重啟迴圈保留（逼人看 log）。

## Rationale

- **D2**：B 相較 A 讓子任務死亡一定冒泡；不選 C，5 個子任務耦合強、不需要各自 policy，solo 維護成本高；不選 D，WS 重連有 rate limit、orderbook state 重建代價高，短暫斷線不該整個重啟；不選 E，PaaS 單容器沒有多 process 預算。代價：子任務 `finally` 若卡住只能等平台 SIGKILL，這條路徑 unit/integration 測不到。
- **D4**：選 registry 而非 process self-report，因為 zombie 正是「process 活著但沒 progress」；不選 Task inspection，等 socket 的 task 狀態是 running 卻無進度；pub-sub 對 5 個子任務是過度設計。代價：每個子任務都要記得在正確位置寫 heartbeat。
- **D5**：keepalive 間隔 = recycle/2（RDS Proxy／HikariCP 慣例），確保 idle 也在 Neon 15 分鐘切斷前換掉連線；sync alembic 是 SQLAlchemy 2.0 官方與 FastAPI template 主流，代價是 image 多一個 psycopg 依賴（~5MB）。
- **D7**：純 long-soak 的弱點是「沒出事 ≠ 修好了」（14 分鐘 bug 經驗），chaos 主動證明修復；SRE 99% 可用性證明（~100h）對 solo 過嚴。
- **D9**：端到端計數驗的是整條 emit chain，內部 counter 只能證明 daemon 自認有送。
- **D10**：原定義數學上必敗；max-gap 抓中途斷流、recency 抓尾段死亡，兩者合起來覆蓋目前 fail mode。不加 CV：1h cell 每小時 1 個 sample 算不出來；不加間隔下限：scheduler 已有 anchor idempotency guard。嚴格 `window_end` 在 self-smoke 自動化後不再 flaky，且保留「手動跑太晚就 fail」的安全側；寬鬆版（`max(ts)+週期`）對跑太晚不敏感。
- **D11**：module 重構而非 subprocess——subprocess 零重構但業務邏輯長住 `scripts/`、測試要繞；代價是一次搬 ~250 行。

## Result

```
git log --oneline eab4b96^..0f3dbe3     # 4.2.0 D1–D6 + 上線後修正
git log --oneline fbdd16c^..83ae38f     # G1 C1 重定義 + self-smoke
```

- 4.2.0 merge `8ecba2c`；上線當天補修：psycopg `ssl=`→`sslmode=`（`8679aeb`）、WS heartbeat 改輪詢 `last_msg_age_ms`（`5011a77`）、candle_writer threshold 90s→65min（`a60db3f`，安靜市場 1h cell 本來就可數分鐘無 candle）。Gate 1 chaos Neon suspend PASS。
- **後續演進（2026-09-27 對照 origin/main）**：TaskGroup、`tenacity`、`pool_recycle=600`、5 分鐘 keepalive、cells.yaml 三層 lookup 仍在。heartbeat 於 `3092d02` 拆成 liveness（可升 fatal／驅動重啟）與 activity（`executor`/`safety_chain`，只 WARN）兩類——因 2026-05-26 canary 在靜市場被 activity heartbeat 觸發重啟迴圈。D9 paper smoke runner 與 D10/D11 G1 gate 於 `d7d2a23`（2026-05-25）隨 Axiom 退役整體移除。部署平台已由 Koyeb 換成 VM docker compose。

## Revocation Triggers

- 若 daemon 拆成多 process 或多容器 → D2 的 one-for-all 範圍與 E 選項需重評。
- 若新增 heartbeat 子任務 → 先判定 liveness 或 activity；把業務活動綁到 liveness 會重演重啟迴圈。
- 若恢復可查詢的 signal sink 並重建 smoke gate → 沿用 D10 的逐 cell max-gap + recency，不回到時間桶。

## Lessons

### Rules

- **R1**：spec 裡的節奏型不變量要先用實際配置代入驗算。`Rule: 寫 continuity／cadence 類驗收條件時，用當前 cells 的 timeframe 算一次是否可能通過，再寫進 spec。`

## Related

- 來源（SDD，repo 內仍追蹤）：
  - `2026-05-18-phase4.2.0-daemon-stabilization-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-19-phase4.2.0-daemon-stabilization.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-21-g1-c1-continuity-redesign-design.md`（原文已不在 repo，本 ADR 即紀錄）
  - `2026-05-21-g1-c1-continuity-redesign.md`（原文已不在 repo，本 ADR 即紀錄）
- 後續：[2026-05-21-phase4.2-safety-harness-and-executor-port](2026-05-21-phase4.2-safety-harness-and-executor-port.md)（姊妹 spec）、[2026-05-20-phase4.3-locf-staleness-budget](2026-05-20-phase4.3-locf-staleness-budget.md)（`stale_exceeded` 不升 fatal 的 carve-out）、[2026-05-25-event-store-3c-axiom-removal](2026-05-25-event-store-3c-axiom-removal.md)（smoke gate 退役）
