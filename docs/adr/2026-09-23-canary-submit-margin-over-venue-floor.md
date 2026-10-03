---
title: 送單金額帶 0.5% 餘裕，不壓在交易所會自行重算的下限上
date: 2026-09-23
status: active
amends: "[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)"
tags: [bfx-funding-bot, decision, execution, canary, venue, capital]
---

# 送單金額帶 0.5% 餘裕，不壓在交易所會自行重算的下限上

## Context

[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 的 D8 規定 funding minimum 依「官方 USD 150 ＋
Bitfinex public UST→USD FX」在本機推導，並明訂**送出前重驗、不默增 exact amount**；代價一欄已寫明
「內部估值／時點差異仍可能導致 venue 拒單且一次性 permit 已消耗」，Revocation Triggers 也寫了
「持續 minimum 拒單 → 停止新增命令並重評 D8；不能擅自增加 amount」。

這個代價在 2026-09-21 22:00 與 09-22 07:49 兩度成真：canary 以 `150 / fx` 推得的 150.02850542、
150.02925571 送出，Bitfinex 皆回 HTTP 500、查無掛單。已用直接證據排除權限（`['funding',1,1]`）、
簽章、資金位置與 nonce；`/funding/offers/hist` 顯示成功過的單 rate／period／type 形狀完全相同，
唯一差別是金額：成功過的單明顯高於下限，失敗的兩筆貼著下限（約 150.03）。

## Options Considered

- **A. 維持 D8：精確下限、不增額**：最小真錢曝險、與規則一字不差；代價是交易所以自己的匯率與時點
  重算同一個 USD 下限，結果落在哪一側接近擲硬幣，而每次落錯邊要付一次 canary、一個 DR 窗口與一次人工裁決。
- **B. 固定比例餘裕（採用）**：送單金額 = 下限 × 1.005；規則本身（`minimum_amount`、`validate_amount`）不變。
- **C. 取得交易所內部估值契約再精算**：理論上最精確；但該契約未公開，等於把放貸無限期擱置在一個拿不到的輸入上。

## Decision

- **D8'（amends D8）**：`RULE.submit_margin = 0.005`，新增 `submit_amount()` 作為**所有實際送出金額**的唯一來源
  （canary prepare／authorize 與 reconciler 的 `min_fill`）；`minimum_amount` 仍是精確規則，
  `validate_amount` 仍以它為最終判準——規則沒有被放寬，只是送出的量不再貼著邊界。
- 餘裕落在 `consume()` 既有的 1% 接受區間（`RELEASE_MINIMUM_TOLERANCE`）內，FX 在 authorize 與送單之間的
  漂移容忍度不受影響（兩端同乘 1.005）。
- 同一 commit 另保留交易所的拒絕理由（見 [2026-09-23-venue-refusal-kept-before-classified](2026-09-23-venue-refusal-kept-before-classified.md)）。

## Rationale

- **選 B 而非 A**：A 的前提是「拒單偶發」，實測 2/2；而 B 上線後第一筆（下限 × 1.005）當場成交
  （`EXECUTED`）。每筆 canary 多承擔下限 0.5% 的曝險，
  換掉的是整輪 release ceremony 的重跑成本，不對稱得很明顯。
- **選 B 而非 C**：C 在可預見的未來拿不到；B 的 0.5% 比任何合理的穩定幣換算差異寬兩個數量級。
- **放棄了什麼**：canary 不再是「最小可能真錢」而是「最小可能＋0.5%」；以及 D8 原本那句「不默增 exact amount」
  的字面承諾。

## Expected Outcome

- canary 與一般送單不再因貼著下限被交易所拒絕。
- `validate_amount` 仍拒絕低於精確下限的任何金額。

## Followup

- 以之後的送單觀察 0.5% 是否足夠或過寬；任何一次「帶餘裕仍被以金額理由拒絕」即重評。
- 交易所 5xx 的業務拒絕分類（D2）見 [2026-09-23-venue-refusal-kept-before-classified](2026-09-23-venue-refusal-kept-before-classified.md)。

## Revocation Triggers

- 帶餘裕的送單仍出現以金額為由的拒單 → 重評餘裕大小或 FX 來源。
- Bitfinex 公布 funding 引擎的換算契約 → 改採 C。

## Lessons

### Rules

- **R1**：這次是在「方向經 Will 核准」的前提下改了送單金額，但動手前**沒有先查既有 ADR**，事後才發現 D8 的
  Revocation Trigger 明文禁止擅自增額——ADR 本身已預見並接受這個代價。核准本身沒有錯，錯在核准時兩邊都
  不知道有一份明文相反的裁定。`Rule: 改動送單金額、風控閘或 halt 語意這類金流行為前，先 rg 該專案 ADR
  是否有明文規定；有就寫 Amendment 再改，而不是先改再補。`

## Related

- 修訂 [2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) 的 D8（funding minimum 不增額）
- 實作 commit `c185a13`（bfx-funding-bot，分支 `codex/operator-onboarding`）
- 驗證：B 上線後首筆 canary 於 2026-09-23 02:08 成交（offer／loan id 不公開）
- 本 ADR 即原始紀錄，無外部來源文件
