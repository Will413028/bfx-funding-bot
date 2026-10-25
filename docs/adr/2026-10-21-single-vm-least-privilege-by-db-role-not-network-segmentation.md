---
title: 維持單一 VM、不做網路分段；最小權限改由每個工作自己的 DB role 承擔
date: 2026-10-21
status: active
amends:
  - "[2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md) Followup 的「網路分段」批次"
tags: [bfx-funding-bot, decision, security, networking, postgresql, operations]
related-commits:
  - "3d33daa6^..6a2104fa"
---

# 單一 VM：DB role 分權取代網路分段

## Context

[2026-10-15](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md) 的 Followup 排了一批「網路分段」：研究用的第二個 Postgres、多個 Docker 網路、bot listener 拆分、可達性矩陣、監控改推送、egress proxy、移除主機 `127.0.0.1:5432`。2026-10-21 開工前重新對照業界做法，發現這台 VM 的主要風險不在網路：開發用的 `ubuntu` 帳號有 sudo、homelab 的 log 收集容器掛 `docker.sock`、cadvisor 是 privileged、公開 app 與 bot 用同一個 Docker daemon。這些工作負載都等同 root，網路分段擋不住。另一方面，研究、週報與 sim 都用 owner 或 bot 的 DB 帳號，權限遠大於需要。

**約束**：

- `external` 單一 maintainer；任何機制都要他能長期維護。
- `external` 研究回測要讀正式環境的 `funding_candles`、`funding_stats`（策略研究的輸入），每週更新。
- `external` 只有 owner 自己的小額資金與一把 Bitfinex key。
- `inherited` 單一 VM 自托（[2026-09-04](2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore.md) 的單機前提）：仍成立，成本與維護量都按一台機器算。
- `inherited` `operator` CLI 容器直接接資料網路（2026-10-15 D4a）：仍成立；2026-10-18 Amendment 規定改到它的網路要先演練。

## Options Considered

- **基準. 依敏感度分段容器網路、服務間最小可達性、限制 egress**：NIST SP 800-190〈Application Container Security Guide〉§4.4、CIS Docker Benchmark（自訂網路、不掛 `docker.sock`、不跑 privileged）。
- **A. 照原 Followup 全做**：第二個 Postgres、多網路、listener 拆分、可達性矩陣、監控推送、egress proxy、移除 5432，必要時分 VM。
- **B. 維持單一 VM 與扁平網路；研究與週報用新 role `bfx_research`、sim 用 `bfx_sim`，都在同一個 Postgres；開發帳號保留 sudo（採用）**。
- **C. 第二個 Postgres 給研究**（A 的一部分單獨做）：研究與正式資料完全隔開。

## Decision

- **D1 = 計畫 D14**：選 B。`bfx_research` 只 SELECT 需要的表，只能寫 `funding_stats`、`attribution_weekly`、`perp_funding_rates`、`liquidations`、`fill_rate_stats`；`bfx_sim` 只能連 `bfx_sim` database。migration `REVOKE ALL` 後精確 GRANT，權限由測試鎖住；`bfx` database 收回 PUBLIC 的 CONNECT。
- **D2**：A 的其餘項目不做。`operator` 容器的網路不變，2026-10-18 Amendment 的演練條件不觸發。
- **D3**：接受的殘餘風險：扁平網路上正式環境的容器連得到 bot 的 8080（`/admin` 要 token）；主機上的開發帳號與監控容器等同 root。逐條寫在 `docs/security/asvs-l2.md`「單一 VM 的扁平網路與主機等同 root」。

## Rationale

- **D1**：B 用一個 migration 就把研究、週報、sim 的權限縮到需要的表，研究程式有 bug 或被污染時改不到帳本與交易狀態。不選 C：研究要讀正式的 candles 與 funding_stats，獨立 DB 就要每週搬資料，多一條會壞的管線。
- **D2**：A 的網路分段擋的是「一個容器被攻破後橫向移動」，但這台主機上已有多個等同 root 的工作負載，攻破任何一個就不需要橫向移動；其餘項目不做的理由是管理成本，單一維護者維護不了（2026-10-21 Will）。選 B 而非 A 的**代價**：扁平網路與主機等同 root 都是明文接受的風險，不是已解決。業界基準的做法要等工作負載與維護人力都變了才划算。

## Result

- `git log --oneline 3d33daa6^..6a2104fa`（PR #244）。2026-10-21 部署，migration 已套用；VM 建了 `bfx_research` 與 `research.env`，手動跑週報成功、12 份報告。
- 權限測試：`bfx_research` 寫 ledger 與 `trading_state` 被拒、`bfx_sim` 連 `bfx` 被拒；GRANT 多一張 bot 表的 mutation 讓測試失敗。
- `bfx_sim` role 等下次跑 sim 前才建（`docs/runbooks/simulation-soak.md` §2b–2c），避免閒置的登入密碼。

## Revocation Triggers

- 開始保管外部使用者的資金或交易所憑證 → 重評分段、egress 限制與 bot 獨立主機。
- 主機上等同 root 的工作負載被移走（開發改在別台、監控不再掛 `docker.sock`）→ 網路分段的收益變大，重評 A。
- 有第二位維護者 → 重評 A 的管理成本前提。

## Related

- 壓縮自 gitignored 的品質基線 Prompt 3 計畫（2026-10-12 起）的 D14 與當時的業界基準草稿；該目錄從未進版控，內容僅存本 ADR。
- `docs/security/asvs-l2.md`（接受的缺口）、`backend/ARCHITECTURE.md` §10a（授權與通訊矩陣）、`docs/runbooks/fresh-host-setup.md`（role 建立）。
