---
title: 公開站的上傳 unit 自己 build、非 root 上傳；Pages token 維持帳戶層級；fUSD 資料等開啟 fUSD 時恢復
date: 2026-10-24
status: active
amends:
  - "[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D6（公開頁用預先產生的 static 檔）"
tags: [bfx-funding-bot, decision, public-site, cloudflare, security, operations]
related-commits:
  - abd5b26c
---

# 公開站：自己 build、非 root 上傳、token 範圍與 fUSD

## Context

[2026-08-31](2026-08-31-account-isolated-execution-target-architecture.md) D6 讓公開頁使用預先產生的 static 檔，不直接查 money DB；2026-10-23 起由 VM 每週產生、上傳到 Cloudflare Pages。上線後的設計審查（2026-10-24）提出三題：

- 第一版的上傳程序以 root 執行，讀 uid 1000 可寫的目錄，靠逐層 directory fd＋`O_NOFOLLOW` 防 symlink；它接在每週報告鏈的 `OnSuccess=` 後面，以 `generated_at` 在 26 小時內推斷 build 成功。
- 上傳 token 是帳戶層級的 Pages Write，同一個 Cloudflare 帳戶還有別的 Pages 專案。
- fUSD 的 1h candle 在 DB 停在 2026-09-09；推論原因是 fUSD 不在 live cells，bot 只寫自己 cells 的 candle（未逐行查證）。

**約束**：

- `external` Cloudflare 的 API token 沒有「只限一個 Pages 專案」的權限，最小是帳戶層級 Pages Write（`docs/runbooks/secret-rotation.md`「Cloudflare Pages token」）。
- `external` `bfx_research` 對 `funding_candles` 只有讀（[2026-10-21](2026-10-21-single-vm-least-privilege-by-db-role-not-network-segmentation.md)）。
- `inherited` 放貸全自動、公開站每週自動更新：仍成立，不能靠人工補資料。

## Options Considered

### build 與上傳的結構

- **基準. build 與部署在同一個步驟，build 失敗即部署失敗；部署程序最小權限，秘密以 credential 機制交付**：systemd `DynamicUser=`、`LoadCredential=`（systemd.exec 文件）。
- **A. 現狀**：root 上傳、逐層 dir fd 防護、接在每週鏈後面、26 小時推斷。
- **B. `bfx-public-site` 先跑一次性 build 容器再上傳，build 失敗 unit 就失敗；上傳改 `DynamicUser` 加 `LoadCredential=`；拿掉 26 小時門檻與每週鏈的耦合（採用）**。

### token 範圍

- **A. 帳戶層級 Pages Write＋限定 VM 出口 IP（採用）**。
- **B. 另開一個只放公開站的 Cloudflare 帳戶**：帳戶層級就等於專案層級。

### fUSD 資料

- **A. 維持現狀：公開 CSV 標出截止日，等開啟 fUSD 放貸時自然恢復（採用）**。
- **B. 每週鏈加 fUSD candle 回補**：`bfx_research` 要 migration 開寫權限。
- **C. 公開站拿掉 fUSD**。

## Decision

- **D1 = 計畫 D17 Q2**：選 B（PR #293）。`bfx-public-site.timer` 每週一 05:47 UTC，自己 build、自己上傳。
- **D2 = 計畫 D17 Q4**：選 A。
- **D3 = 計畫 D17 Q5**：選 A。開啟 fUSD 時，把每日檢查的 fUSD 新鮮度加回去。

## Rationale

- **D1**：A 的安全靠「root 讀別人可寫的目錄」時每一步都不出錯，正確但脆弱；成功與否用時間推斷，build 壞掉只會表現為「資料舊了」。B 讓失敗在發生的那一步就失敗，上傳程序拿不到 root、DB 與 docker。**代價**：每次上傳多跑一個容器，結構改動一次做完。
- **D2**：B 需要另建並管理一個帳戶，換來的隔離只在「同帳戶有第二個會自動部署的 Pages 專案」或「token 離開 VM 使用」時才有差；現在兩者都不成立，而 IP 限制讓 token 外洩後無法從別處使用。**代價**：token 外洩時能改同帳戶其他 Pages 專案，要先撤銷（runbook 已寫）。
- **D3**：B 為了暫停中的幣別擴大研究帳號的寫權限，違反剛縮好的最小權限；C 丟掉公開站已有的歷史資料。A 的**代價**：fUSD 的公開資料在開啟前一直停在截止日，讀者要看標示。

## Result

- `git log --oneline -1 abd5b26c`（PR #293）；獨立 review 0 HIGH、2 MED、7 LOW 全修。VM 上 timer 已啟用，手動執行 `published 6 files`；舊的 build 目錄已刪。
- 公開 CSV 標出 fUSD 截止日（PR #281）。
- 如預期。

## Revocation Triggers

- 同一個 Cloudflare 帳戶出現第二個會自動部署的 Pages 專案，或 token 被用在 VM 以外 → 改用獨立帳戶（D2 的 B）。
- fUSD 開始真錢放貸 → 每日檢查加回 fUSD 新鮮度；若 candle 仍不更新，回頭查 bot 寫 candle 的範圍。

## Related

- 壓縮自 gitignored 的品質基線 Prompt 3 計畫（2026-10-12 起）的 D17 Q2、Q4、Q5 與步驟 16 的設計審查；該目錄從未進版控，內容僅存本 ADR。
- `docs/runbooks/operations.md` §8「公開站」、`docs/runbooks/secret-rotation.md`「Cloudflare Pages token」、`docs/runbooks/launch-checklist.md`。
