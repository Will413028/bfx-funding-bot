---
title: ASVS 對照整份 L2、全部到終態才公開；威脅模型不進 public repo；個資先寫政策、實作等第二個使用者
date: 2026-10-15
status: active
tags: [bfx-funding-bot, decision, security, asvs, disclosure, privacy]
related-commits:
  - "3b86ea8a^..111fb8a8"
---

# ASVS L2 的範圍與在 public repo 的揭露方式

## Context

live 級（真錢、持有交易所憑證）的品質要求包含兩件事：碰錢的 repo 要做到 OWASP ASVS L2 對照，以及個資的保存、刪除、外洩通報（P3）。2026-10-12 第一次對照與威脅建模，結果是 84 條不符合，其中有從公開入口就能利用的項目。原計畫把兩份文件放進 `docs/security/`，但 2026-10-15 確認 repo 是 public。

**約束**：

- `external` repo 是 public；`SECURITY.md` 要求漏洞私下回報。修補 PR 一合併就等於揭露該項。
- `external` 單一 operator，目前個資只有 operator 本人的；對外 SaaS 未上線（第二個使用者要等 08-31 的 SP6）。
- `inherited` 品質頁的級別規定（live 級做整份 L2 與 P3）：我方規則，仍成立的理由是 bot 已用真錢運作。

## Options Considered

### 對照範圍

- **A. 整份 L2 逐條對照（採用）**：L1＋L2 共 253 條。
- **B. 只對照認證、session、存取控制三章**，其餘延到 Phase B。

### 揭露方式

- **基準. 協同揭露：修好之前不公開細節，修好之後才公開或發 advisory**：ISO/IEC 29147、GitHub security advisories 的做法。
- **A. 修補前留在不進版控的附件，修補階段結束後兩份都進 `docs/security/`**：被接受的風險也會一起公開。
- **B. ASVS 對照在每一條都到終態後才進 `docs/security/`（只寫判定與控制，接受項寫理由）；威脅模型永遠不進 public repo，由 maintainer 保管（採用）**。
- **C. 現在就公開**。
- 不用 GitHub private security advisory：它一個漏洞一則，不是放 253 列對照的地方。

### 修補階段結束時還沒到終態的條目（2026-10-24）

修補階段結束時 44 條不符合裡約 35 條既沒修也沒接受，B 的前提不成立。

- **A. 只發布符合、不適用與已接受的條目**，未決的留在私下。
- **B. 先逐條裁決（修、接受附理由、或因架構消失改判），全部到終態後整份發布（採用）**。
- **C. 照原文整份發布**：等於公開哪些控制還沒做。

### 個資（P3）

- **A. 現在寫一段簡短政策：有哪些資料、外洩怎麼通報、帳號怎麼刪除；實作等 SP6（採用）**。
- **B. 整項延到 SP6**：偏離品質頁，理由是自訂的。

## Decision

- **D1 = 計畫 D6**：選 A，整份 L2。
- **D2 = 計畫 D9**：選 B。tracked 的 ADR 與 PR 描述只寫決定與修補內容，不寫未修補弱點的利用方式。
- **D3 = 計畫 D17 Q1**：選 B。30 條逐條裁決：修 26、改判符合 2、接受 2，修完才發布。
- **D4 = 計畫 D7**：選 A，寫在 `backend/ARCHITECTURE.md` §10f。

## Rationale

- **D1**：live 級本來就包含 prelaunch 要求的整份 L2；單一 operator 下很多條是不適用，可以快速帶過。B 的工作量約三分之一，**代價**是其餘章節的缺口要到 Phase B 才會被看見。
- **D2**：B 而非 A：A 會把威脅模型裡接受的風險（例如帳號被盜後多快能上線一版 image）一起公開；ASVS 對照只寫判定與控制，接受項只寫「接受」與理由。不選 C：當時有可從公開入口利用的項目，業界做法是修好之前不公開。**代價**：外部讀者看不到威脅模型，只能從 `docs/security/README.md` 知道它存在。
- **D3**：A 會讓「沒列出來」本身透露哪些控制缺；C 直接公開缺口。B 晚一點發布，**代價**是多一輪修補（PR #295–#303）。
- **D4**：品質頁的 P3 不看有沒有 pii 旗標，只看真實資料是否在 production；現在寫政策成本低，實作（刪除流程、通報對象）要等真的有第二個人的資料才有意義。不選 B：延後的理由是自訂的。

## Result

- `git log --oneline --first-parent 3b86ea8a^..111fb8a8`：ASVS 修補批次與公開版。修補 PR #295–#303，公開版 PR #304（`docs/security/asvs-l2.md`，快照 `9ddd941f`）：253 條中符合 130、不符合 17（全部已接受，附補償控制與重評觸發條件）、不適用 106、待證 0。
- 個資段 PR #274（`backend/ARCHITECTURE.md` §10f）。
- 威脅模型由 maintainer 在 repo 外保管；`docs/security/README.md` 說明它存在。

## Invariants

- 能利用的發現先修、再寫進 tracked 文件；修補 PR 的描述只寫修了什麼。
- `docs/security/asvs-l2.md` 的「不符合」全部是已接受的缺口，各有理由與重評觸發條件。

## Revocation Triggers

- 開放第二個使用者（SP6）→ 實作 P3 的刪除與外洩通報，並重做 ASVS 對照（多使用者的條目不再是不適用）。
- 保管外部使用者的資金或憑證 → 重評 L3 的必要性。

## Related

- 壓縮自 gitignored 的品質基線 Prompt 3 計畫（2026-10-12 起）的 D6、D7、D9、D17 Q1；該目錄從未進版控，內容僅存本 ADR。
- [2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md)：這次對照引出的目標架構。
- `docs/security/asvs-l2.md`、`docs/security/README.md`、`SECURITY.md`。
