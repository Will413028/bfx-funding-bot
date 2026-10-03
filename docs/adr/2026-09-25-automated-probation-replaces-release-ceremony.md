---
title: 放行真錢改為「變更分級＋執行期限額」，取代每個 build 的人工 canary ceremony
date: 2026-09-25
status: "superseded-by: [2026-09-25-lending-envelope-replaces-probation-and-account-halt](2026-09-25-lending-envelope-replaces-probation-and-account-halt.md)"
amends:
  - "[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)"
  - "[2026-09-22-maintenance-halt-without-canary](2026-09-22-maintenance-halt-without-canary.md)"
  - "[2026-09-23-operator-renews-spent-canary-epoch](2026-09-23-operator-renews-spent-canary-epoch.md)"
  - "[2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md)"
tags: [bfx-funding-bot, decision, safety, deployment, canary, disaster-recovery, risk-control]
---

# 放行真錢改為變更分級＋執行期限額

## Context

prior state：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D4 讓每個 build 都要人走四步 ceremony
送一筆真錢 canary；promote 是唯一寫 `halted=False` 的路徑（`release_worker.check_normal`）；部署只在
halted 下運作；每一步都要 900 秒內的 DR 收據（`halt2_cutover._MEASUREMENT_MAX_AGE_MS`），刷新收據要
停下 app 跑完整隔離還原。結果：2026-09-20 開五個窗口四個過期；本週 4 次守衛組合死結
（fail-closed guard 組合死鎖）；
放貸自 2026-08-05 halted 至今。2026-09-25 `2730cbd` 已部署但不跑舊 ceremony。

2026-09-25 Will 指示：系統尚未上線，一次重構到業界 best practice、不留技術債。同日查證的業界依據
（出處見 Related）：法規與業界協會把人工授權放在「部署或重大更新」，深度與變更大小成比例；執行期
風控自動、永遠開著；新部署先在預設限額下運行；DR 是定期自動測試，沒有任何來源把還原演練綁在每次
部署或放行上。

## Options Considered

- **A. 維持現狀**：每個 build 人工送真錢 canary；代價如上，且放貸取決於 Will 是否在場。
- **B. 只修機械面**：DR 收據改定期、ceremony 工具化；仍對所有變更一視同仁、每次部署都要人在場。
- **C. 自動 probation（本 ADR 前一版）**：把人換成機器，但每個 build 仍被當成重大變更，DR 超標仍擋放貸。
- **D. 變更分級＋執行期限額（採用）**：standard 變更全自動；material 變更一次核准＋受限額度期；
  風控、kill switch、告警常駐；DR 與發版脫鉤。
- **E. 取消所有部署期限制**：09-22 已否決（新 build 首筆單值得有界驗證）；D 對 material 變更保留限額期，不是 E。

## Decision（2026-09-25 Will 拍板：D、25%／24h／3 筆、Telegram）

- **D1 變更分級**：CI 依變更路徑自動判定 standard／material，只能升級不能降級。material＝下單路徑、
  策略／定價、風控參數、CapitalPolicy、不可逆 migration、新幣別／產品；其餘（前端、文件、觀測、研究
  腳本、測試）為 standard。
- **D2 部署一律自動、放行依分級**：standard → 部署後自動回到部署前的 trading state。material →
  部署後停在 REDUCING（不開新單，既有 offer／loan 照常管理），operator 在 UI 以 TOTP 按一次核准。
- **D3 受限額度期**（RTS 6 Art 8、FIA 2012 post-deployment）：核准後 exposure 上限＝正常 cell limit 的
  25%（至少容納一筆最小單）；bake 24 小時且 ≥3 筆 ACK、期間沒有任何自動 HALTED 觸發 → 自動放寬到
  正常 CapitalPolicy；任一失敗 → 自動 HALTED。取代四步 ceremony、canary permit 與 epoch 續開。
- **D4 trading state 與發版脫鉤**：ACTIVE／REDUCING／HALTED 為獨立的 ops 控制。halt 由 operator、
  kill switch 或自動保護觸發；resume 是一次 TOTP 認證動作，自動保護觸發後的 resume 進入 D3 限額期。
  HALTED（手動 kill switch 或自動保護）＝寫停機＋對 venue 呼叫 funding cancel-all，包含 orphan 與
  UNKNOWN 未解的幣別，只需 writer lock、不經其他 guard；維運暫停用 REDUCING：只准撤單，不掛新單也
  不重掛（2026-09-25 Will 拍板）。
- **D5 執行期風控常駐、fail-closed**：保留 CapitalPolicy、action-size clamp、book guards、E2 rate
  clamp；對照 RTS 6 Art 12／15／16／17 補齊缺口（單筆上限、利率 collar、送單節流），並加即時告警：
  halt、UNKNOWN、reconcile divergence、自動保護、備份失敗，秒級推播到 Telegram。自動 HALTED 的觸發：
  UNKNOWN submit、orphan、無法歸屬的承諾、掛單金額不符或 venue 借出額高於內部帳、loss limiter、
  writer lock 遺失。借款到期由 reconcile 補回的 realized 下降屬預期，不觸發（過去 30 天 5 次
  divergence 全是這類）。
- **D6 DR 與發版脫鉤**：WAL 持續歸檔＋freshness 告警；還原測試每月自動（以 `event_prefix_hashes` 前綴
  比對，不必停寫入者）並在 PG 升級、備份設定／憑證變更、不可逆 migration 前加做，成功寫 heartbeat；
  每年一次完整演練（2026-09-25 Will 拍板：每月而非每週）。部署 preflight 讀「最近成功備份是否在 RPO 內」；含 migration 或 DR 設定變更的部署，先備份
  並通過一次還原測試才套用。不發 DR 收據，DR 狀態不擋交易或 resume。
- **D7 單人四眼的替代**：資金放 Bitfinex sub-account；API key 無提領權＋IP 白名單；append-only release
  ledger 與 audit；每季自我檢視限額、告警與還原測試紀錄。
- **保留**：durable intent 後才送單、UNKNOWN 不自動重送（funding submit 無 `cid`）、reconcile 為曝險
  權威、single writer、D4' outbox、啟動時先對齊 venue 狀態再開新單。

## Amendment（2026-09-25 獨立設計審查後）

- **D1**：分級改在 VM 上由部署工具對 diff 判定（規則取目標 commit；只升不降；operator 可強制 material），
  比較基準是「最後一個被接受的版本」而非上一次部署；bot 累積未消化的 material，堵住「material 後接 standard」的繞過。
- **D4**：刪除 `BFX_KILL_SWITCH` env 入口；持久化停機走 UI kill 或 static `/admin/halt`，不依賴 DB 的 break-glass 是停容器。
- **D6**：刪除「部署 preflight 讀 RPO」（Will：備份新鮮度已有告警）；含 migration 的部署順序改為先停 bot → 備份 →
  還原測試 → migrate → 起新版，失敗維持停機、只 roll forward（Will：選此而非 expand/contract）。
- **D3 否決帳戶總上限（Will，上線當晚）**：以當時的小額資金，25% cell 上限低於 venue 最小單，每個 cell
  都放寬到一筆最小單，兩 cell 合計佔帳戶資金大半，限額期幾乎不限量。選維持按 cell 計而非加帳戶總上限：加上限要再一次
  material 部署與核准、且資金放大後 25% 自然高於最小單；代價是小資金期的限額期只驗「能送出」而非「小額試水」。

## Rationale

- **選 D 而非 A**：RTS 6 Art 5 要求授權的是「deployment or substantial update」，不是每個 build。
  DORA 2019：需外部核准的重量級流程，受訪者成為 low performer 的機率高 2.6 倍，且沒有更低的變更
  失敗率。現行 canary 只看一筆最小單幾分鐘，遠短於一個放貸週期（掛單→成交→期滿），而 SRE Workbook
  指出這種 before/after 比較訊號很弱。
- **選 D 而非 B**：B 仍「treating all changes equally」（DORA 列為 pitfall），每次部署仍依賴單一人。
- **選 D 而非 C**：C 讓改 CSS 也要走 probation，並把 DR 當放行閘門；兩者都沒有來源支持。D 把人工
  核准留在法規放的位置（material），而且只要按一次。
- **代價**：(1) 分級誤判時 material 會以正常額度上線，以「CI 只升不降」和常駐限額兜底；(2) 受限額度期
  24 小時會壓低收益；(3) 備份失效期間 VM 若故障，會遺失最近的本地事件——曝險可由 venue reconcile
  重建，但稽核事件會有缺口，改以告警＋修復處理，而非停止交易；(4) 實作量大：刪除約 2,900 行 ceremony
  機制、27 個測試檔、4 份 runbook，新增 state machine、告警與排程，期間放貸維持 halted。

## Expected Outcome

- standard 變更：merge 後零人工步驟，自動部署、自動恢復。
- material 變更：一次 TOTP 核准，24 小時後自動放寬到正常額度。
- 不再有收據過期、epoch 用完這類死結；事件發生後秒級收到告警。

## Followup

- Phase 0 盤點：現有執行期風控對照 RTS 6 Art 12／15／16／17；Bitfinex key 權限、IP 白名單、sub-account。
- Phase 1 供應鏈：見 [2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)。
- Phase 2 治理：CI 分級規則、trading state 機、核准＋受限額度期、kill switch、D4' outbox 同批；刪除
  release_sessions ceremony、permit、epoch 續開、halt2 DR 閘門與前端 release 面板。
- Phase 3 監控與 DR：告警推播、每月＋事件觸發的還原測試＋heartbeat、備份 freshness 告警。
- Phase 4 文件：重寫 runbook，刪除 halt-1／halt-2／immutable-release／release-0 的過時段落與 VM 上 repo 外腳本。
- 放貸恢復 gate：Phase 1、Phase 2 與 Phase 3 的告警完成 → Will 以 TOTP resume halt 11 → 進入 D3 限額期。

## Invariants

- material 變更未核准前不開新單；限額期通過前 exposure 不超過上限。
- 分級只能由 CI 升級，人或 commit 訊息都不能降級。
- 自動保護觸發的 HALTED 不自動解除。
- DR 狀態不擋交易或 resume；含 migration 的部署必須先有成功備份與通過的還原測試。

## Revocation Triggers

- 某次 standard 變更造成資金損失或下單異常 → 重審分級規則。
- 通過限額期後在正常額度下出事 → 重審 bake 條件。
- 開放外部資金（SaaS 多租戶）→ 四眼改為真正的第二人，治理重評並另查法規。

## Related

- 來源：2026-09-25 與 Will 討論，本 ADR 即原始紀錄。業界依據（當日實際查證原文）：MiFID II RTS 6
  (EU) 2017/589 Art 5／8／12／14／15／16／17（eur-lex CELEX:32017R0589）；SEC Rule 15c3-5；FIA《Best
  Practices for Automated Trading Risk Controls》(2024)；FIA PTG《Software Development and Change
  Management Recommendations》(2012，收錄於 CFTC TAC 2012-10-30 reference)；FCA 2025 algorithmic trading
  controls multi-firm review；Google SRE Workbook Ch.16《Canarying Releases》；DORA State of DevOps 2019
  pp.49–51；AWS Well-Architected REL08-BP05、REL09-BP03／04、REL13-BP03；SRE Book Ch.26；NIST SP 800-34
  Rev.1；Freqtrade、Hummingbot、NautilusTrader 官方文件。
- 修正對象：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D4、[2026-09-22-maintenance-halt-without-canary](2026-09-22-maintenance-halt-without-canary.md)、
  [2026-09-23-operator-renews-spent-canary-epoch](2026-09-23-operator-renews-spent-canary-epoch.md)、[2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md) 的 Halt 2 DR 收據綁定。
- 同批：[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)；[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)（D4' outbox）。
- 實作 plan（T1–T12，含 Phase 0 盤點後的任務表與進度 SHA）：`2026-09-25-release-governance-refactor.md`。落地後同日被取代：分級、核准、限額期、REDUCING 由 PR #19 刪除；仍存活的是 D6（每月＋變更觸發 prefix 還原測試 `d75b288`、先停 bot→備份→還原測試→migrate）、D7 key 權限、Telegram 告警 `da63b6f`、ceremony 資料封存 `8422d13`（`release_archive`）（原文已不在 repo，本 ADR 即紀錄）。
