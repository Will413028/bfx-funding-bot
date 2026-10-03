---
title: 放貸風控改為「每筆單條款包絡＋分級反應」，取代變更分級、限額期與帳戶級自動停機
date: 2026-09-25
status: active
supersedes: "[2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md)"
tags: [bfx-funding-bot, decision, safety, risk-control, trading-state, deployment]
---

# 放貸風控改為每筆單條款包絡＋分級反應

## Context

prior state：同日稍早的 [2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md) 以 MiFID II RTS 6／FIA
為依據：部署分 standard／material，material 要 TOTP 核准＋24h／25% 限額期；ACTIVE／REDUCING／HALTED
狀態機；UNKNOWN、orphan、金額不符、venue 借出額＞ledger、loss limiter、writer lock 遺失一律寫
`HALTED/auto`＋venue cancel-all、永不自動解除，resume 再進限額期。Will 指出業界放貸 bot 只要 API key、
保留額與策略設定，要求上線前改成治本做法。

**放貸的傷害模型**：唯一不可逆的傷害是一筆已成交借款的條款（金額×利率×天期），lender 不能召回。
NAV 以幣別原生單位計（`ReconcileNavTracker`＝available＋reserved＋realized），壞利率只會少賺、不會
虧本金；loss／drawdown limiter 只會被提領、轉帳或平台分攤損失觸發，停機也補救不了。

**約束**：

- `external` API key 只有 funding 寫權、沒有提領（`accounts/vault.py` `_ALLOWED_WRITE_SCOPES`）；venue 拒絕
  超過 available 的 offer；已成交借款不可召回；funding submit 沒有 `cid`，結果可能 UNKNOWN；
  `funding/offer/cancel/all` 撤該幣別**所有** offer（含手動掛的）：Bitfinex API。
- `external` 尚未上線、只有 Will 的資金與一名 operator；frontend 經 Funnel 公開。
- `inherited` 先寫 durable intent 再送單、UNKNOWN 不自動重送、reconcile 為曝險權威、single writer：
  仍成立，根源是 venue 沒有 `cid`（external），不是既有程式碼。
- `inherited` 恢復類動作要 TOTP：仍成立，因為控制面對外公開。
- `inherited` 含 migration 的部署先停 bot→備份→還原測試（前 ADR D6）與 sub-account／key 權限（D7）：
  仍成立，與放行治理無關，本 ADR 沿用。
- `inherited`（**丟棄**）「RTS 6／FIA 是適用基準」：它管的是受監管投資機構在交易場所的演算法交易，
  前提是無上限部位風險、市場失序、第三方資金；自有資金在 funding book 放貸三者皆無。
- `inherited`（**丟棄**）「新 build 首筆單值得有界驗證」：前 ADR Amendment 已承認小資金時限額期不限量。

## Options Considered

- **基準. 業界放貸 bot**：只有 API key＋每幣別設定（`mindailyrate`／`maxdailyrate`、`maxtolend`、
  `minloansize`、`xdaythreshold`／`xdays` 天期規則）；錯誤記 log、下一輪重試；沒有核准、限額期或停機
  狀態機。來源：MikaLendingBot（支援 Bitfinex）https://poloniexlendingbot.readthedocs.io/en/latest/configuration.html ；
  Freqtrade Protections 為自動到期的 lock（`stop_duration`）https://www.freqtrade.io/en/stable/plugins/ 。
- **A. 維持前 ADR**：變更分級＋material 核准＋限額期＋帳戶級 auto-HALT。
- **B. 保留狀態機與限額期，只把 auto-HALT 縮到幣別**。
- **C. 基準＋平台必需的補強（採用）**：條款包絡在 transport 邊界不可繞過；異常分級、範圍最小化；
  部署不碰交易狀態。

## Decision（2026-09-25 Will 拍板：C；外來 offer 只管自己的＋建議 sub-account；先做平台層，多租戶留 Phase 5）

- **D1 條款包絡是主控制**：每幣別設定併入有版本的 `CapitalPolicy`（DB，TOTP amend）：`enabled`、
  `reserve_amount`、單筆上限、天期 [min,max]、利率下限＝max(絕對 APR 下限, book 中位數×ratio)、
  open offer 數、送單節流。`safety.live.yaml` 的 `pre_trade_limits` 搬進來、YAML 路徑刪除。每筆 submit
  在 transport 前 fail-closed 全數檢查；低於下限就閒置。Phase 5 的 tenant 設定只能在包絡內收窄。
- **D2 只管自己的 offer**：有 durable intent 來源的 offer／借款才是受管；外來 offer（手動、Bitfinex
  auto-renew）不接管、不撤，金額由 venue available 自然扣除，只告警。runbook 建議 bot 用專屬 sub-account。
- **D3 異常分四級**：
  1. 拒絕單一動作：違反包絡、book stale、auth DOWN、heartbeat。
  2. 幣別隔離、證據到了自動解除：UNKNOWN submit、讀取失敗、未接受的 snapshot。隔離是由未解
     uncertainty 推導出的狀態，不另存；逾時只告警。
  3. 帳戶 `HALTED/auto`、需 TOTP resume：只限 bot 對自己的認知錯了——受管 offer 金額不符、identity
     conflict、送單節流持續觸發（只數 submit）、無法以外來 offer 解釋的 ledger 守恆破壞。
  4. 只告警：外來 offer／借款、NAV 下降。loss／drawdown limiter 失去停機權；writer lock 遺失改為
     process fencing（停寫、退出、由 supervisor 重啟），不寫 trading state。
- **D3a UNKNOWN 結案靠金額指紋（2026-09-25 Will 追加）**：funding 沒有 `cid`，每筆送單金額的末 4 位小數編入
  唯一指紋（避開同幣別未結束的單，改變 <0.0001）。settle 窗口後以完整快照＋歷史結案：找到該指紋 → 認領；
  歷史完整涵蓋卻沒有 → 判定未送出；資料不足 → 維持隔離並告警。取代「禁止以金額／時間比對」的舊規則，
  而非 Mika 式每輪全撤全掛（會撤手動單、沒有稽核）或純人工裁決（隔離形同停機）。
- **D4 operator 控制**：日常用每幣別 `enabled`（關閉＝撤受管 offer，已成交借款到期回收）；緊急用 kill
  （`HALTED/operator`＋cancel-all，明示會連手動 offer 一起撤）。trading state 收斂為 ACTIVE／HALTED，
  刪除 REDUCING。resume 經 TOTP、不帶限額期；static token 只能降低曝險。
- **D5 部署不碰交易狀態**：刪除 material／standard 分級、`deployment_approvals`、限額期與其 trigger、
  bot 端的 deployment ledger 累積判斷。部署工具只保留 migration／DR 設定觸發的備份＋還原測試與回滾規則。
  策略與定價品質在 merge 前由 CI、回測與 `/admin/dry-evaluate` 把關。

## Rationale

- **選 C 而非 A**：包絡界定了每筆單的最壞條款——最壞＝可用資金以下限利率借最長天期，損失是機會成本、
  本金不動；限額期在這之上再乘 25% 沒有新資訊，小資金時還被最小單抵銷。A 的 auto-HALT 多數保護不到
  東西（停機收不回借款、提領觸發 loss limiter、orphan 觸發 cancel-all 撤掉 Will 自己的手動單），代價是
  每次都要人到場。到 Phase 5，operator 不可能替每個 tenant 核准部署或 resume。
- **選 C 而非 B**：B 縮小了誤停範圍，但保留「部署改變交易狀態」這個根本錯配，也保留限額期這個不驗
  策略品質（24h／3 筆看不出放貸收益）的檢查。
- **選 C 而非純基準**：基準是自托單人工具，程式碼風險由使用者承擔、錯誤就重試；平台以同一份程式碼跑
  多個帳戶，而且沒有 `cid` 時盲目重試可能重複掛單並吃進 reserve（venue 只認 available，不認 reserve），
  所以保留 durable intent、D3 第 2、3 級與 kill。
- **D2 而非「錢包全歸 bot」**：獨立草稿指出沒有 `cid` 時分不清「自己的 UNKNOWN 單」與條件相同的外來單。
  兩種解讀都會從 available 扣掉那筆金額，差別只在 bot 是否重定價它；誤判的後果是少管一筆自己的單或碰
  到條件完全相同的手動單，都小於要求使用者交出整個錢包的導入摩擦。
- **不採首筆往返檢查（獨立草稿提議）**：它要 bot 重新知道部署分級，把 D5 刪掉的耦合帶回來；送單路徑錯誤
  已由 reconcile 的金額不符（第 3 級）偵測。
- **代價**：(1) 包絡程式碼是共模單點，新 build 直接以正常額度上線，安全押在包絡的回歸測試；(2) 第 2、4 級
  依賴有人看 Telegram；(3) 絕對 APR 下限要人設，設太高資金閒置、太低失去對全市場低迷的保護；
  (4) 實作量：刪限額期、核准、分級、REDUCING、相關 trigger、測試與前端面板，重寫 runbook 與 ARCHITECTURE §6。

## Expected Outcome

- 部署永遠不改變放貸狀態；手動掛單、提領、UNKNOWN 都不再停掉帳戶。
- 需要人到場的只剩持續不消失或反覆發生的第 3 級事件與 operator kill，每次第 3 級都對應一個 bot bug。
- 任何 build 送出的單都在包絡內；Phase 5 只需把 `CapitalPolicy` 的擁有者從帳戶延伸到 tenant。

## Followup

- ✅ 已落地：PR #19（merge `2f3c70f`）、#20（`d9cf002`）；change_class 欄位由後續 migration `5b9e3d7a2f41` drop。
- ✅ 初值（plan §3）：`min_rate_apr` 1.0% 而非原擬 p10≈3%——fUST p2 近 365 天 p1 0.93%／p5 2.28%／p10 2.96%（近 90 天 p10 1.82%），2 天期借低價代價小、閒置代價大，絕對下限只擋單位錯誤與全市場崩跌，定價交給相對下限（ratio 0.5）；period 2/2、open offers 6、單筆上限不變；第 2 級逾時 30 分鐘告警、之後每 6 小時。是否已 apply 到 production policy 未在本 ADR 查證。
- ✅ lent＞ledger 歸因：先扣上次被接受 snapshot 之後結束的外來 offer 已成交量，扣完仍 >0.01 才第 3 級，否則只告警（見 repo ARCHITECTURE §6）。
- ✅ ARCHITECTURE §6、operations／deploy runbook 已重寫；⚠️ §8 phase 表 `live` 列仍寫「release flow（material 核准＋限額期）」，待修。

## Invariants

- 每筆新 offer 都在 transport 前通過包絡；缺設定就拒絕，沒有 bypass。
- 自動反應永不撤、不接管外來 offer；只有 operator kill 會 cancel-all。
- UNKNOWN 永不自動重送；隔離期間該幣別沒有新 submit。
- 部署不寫 trading state；第 3 級停機只在條件消失時自動解除、operator kill 除外（D4 此點由 [2026-09-26-auto-halt-resumes-when-condition-clears](2026-09-26-auto-halt-resumes-when-condition-clears.md) 取代）。

## Revocation Triggers

- 天期上限拉長（>30 天）或利率下限放寬 → 最壞損失上界等比放大，重評是否加分段曝險。
- 出現包絡內卻可歸因於 bot 的損失 → 重評包絡維度或恢復限額期。
- Phase 5 開放外部資金、或需要監管定位 → 重評部署核准與四眼。
- Bitfinex funding 支援 client id → UNKNOWN 改冪等重送，取消第 2 級隔離。

## Lessons

### Rules

- **R1**：前 ADR 借用演算法交易法規，沒先驗證損失模型相同，結果一天內就被推翻。
  Rule: 引用另一個領域的法規或業界基準前，先寫出本領域不可逆的傷害與最壞損失上界，確認基準的前提成立；
  每個控制都要對應它界定的傷害，對不上的就刪。

## Review Notes

### Amendment Decision（2026-09-26 獨立 design review 後，Will 拍板）

- 撤受管 offer 只有一處、level-triggered：帳戶 HALTED 或幣別停用時，planner 每 tick 按 id 撤到沒有；
  自動保護只寫 HALTED，venue cancel-all 只屬 operator kill。拒絕開機只告警並退出、不寫狀態（否則修好的下一版仍要 resume，違反 D5）。
- 刪除第 3 級「受管 offer 落在包絡外」（包絡已在送單前強制，事後檢查在收緊設定後會誤停）；追加：金額全程 Decimal、gate 記憶體 latch 改為 process 退出、幣別開關走 TOTP UI。
- Carry-over audit（刻意保留、重開條件）：`max_offer_amount` 留在 policy 頂層（Phase 5 或 schema 4 時重評）；「受管」由 classifier 與 `execution_decision_id` 兩層判定（claim 綁 venue id 方式改變或出現第三個 consumer 時重評）；`pathspec` 只為四條 DR trigger（下次改 `DR_TRIGGER_PATTERNS` 換 git `:(glob)`）；reconciler 的 legacy uncertainty fallback（paper/shadow 退場時刪）；trading-state trigger 的 Python 鏡像（規則再增加時重評，parity 測試守住）。

## Related

- 來源：2026-09-25 與 Will 討論，本 ADR 即原始紀錄；另比對兩份獨立 agent 草稿（未存檔），其論點已併入 Rationale。程式碼事實取自 bfx-funding-bot main `d475d51`：`backend/src/bfx_funding_bot/modules/execution/` 的 `safety/protection.py`、`safety/nav_pnl_source.py`、`capital_policy.py`，`backend/configs/safety.live.yaml`。
- 實作 plan（含 §6 carry-over audit）：`2026-09-25-lending-envelope.md`（原文已不在 repo，本 ADR 即紀錄）。
- 業界基準：見 Options「基準」兩個 URL。
- 取代：[2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md)（D6 DR、D7 key 沿用）。
- 同批：[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)（其「放行依分級規則」一句由 D5 取代）。
- Lessons R1：對應的通用教訓為「質疑繼承來的安全機制」（本案為其中一例）。
