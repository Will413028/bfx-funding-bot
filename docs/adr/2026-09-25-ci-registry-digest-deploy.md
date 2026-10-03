---
title: Release artifact 改為 CI build＋registry digest 部署，取代筆電 build／tar／rsync 與 VM 上 build
date: 2026-09-25
status: active
amends:
  - "[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md)"
tags: [bfx-funding-bot, decision, deployment, supply-chain, container]
---

# Release artifact 改為 CI build＋registry digest 部署

## Context

prior state：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D5 在開發者筆電 build，`docker save` 成
tar、rsync 1.5GB 到 VM，再以自製程式驗證 archive（`image_artifact.py`、`release_package.py`、
`immutable_release.py` 約 850 行）；D7 以 host 簽發的 launch receipt 證明 runtime 身分
（`core/release_identity.py`）。2026-09-22 Amendment D5'：Will 裁定改在 VM 上 build（尚未實作）。

2026-09-25 部署 `2730cbd` 的實際成本：筆電磁碟滿導致 Docker Desktop 卡死兩次、要強制重啟；VM 磁碟
95% 的主因是早期在 VM 上 build 留下的 34.75GB build cache。同日 Will 指示一次重構到 best practice。

## Options Considered

- **A. 維持 D5**：artifact 可驗證；代價是筆電在供應鏈上、每次傳 1.5GB，自製驗證重做了 registry
  digest 本來就提供的保證。
- **B. D5'：VM 上從 git commit build**（Will 2026-09-22 裁定，未實作）：省掉傳輸；代價是 build 與
  run 在同一台機器（SLSA Build L0），build 工具與 cache 常駐 production 主機，與 bot 搶 CPU、RAM、磁碟。
- **C. CI build → registry → VM 以 digest pull（採用）**：GitHub Actions arm64 runner（private repo
  可用免費分鐘）原生 build、push 到 registry，VM 只 pull 指定 digest；代價是 GitHub 成為供應鏈的一環、
  registry 儲存費用、VM 需要唯讀拉取憑證。

子選項：
- **registry**：GHCR（建議，免維運）／VM 本機 registry（無費用，但 CI 要能連進 VM）。
- **簽章**：cosign 現在就簽／先做 digest pin＋只有 CI 有 push 權限，簽章延到多租戶（建議後者）。
- **觸發部署**：VM 端 timer 拉取（建議，prod 不必開放 CI 連入）／CI 經 Tailscale SSH 推送。

## Decision（2026-09-25 Will 拍板：C、GHCR、簽章延後、VM timer 拉取）

- **D1**：只有 CI 從 `main` 的 commit build，push 到 GHCR；production 只執行以 digest pin 的 image，
  部署流程中不使用任何可變 tag，VM 上不 build。
- **D2 部署**：VM 端 timer 偵測 `main` 最新成功的 release manifest（commit、digest、分級）→ 以 digest
  pull → 需要 migration 時先備份再套用 → 換容器 → 健康檢查 → 失敗自動 rollback 到上一個 digest。
  放行依 [2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md) 的分級規則。
- **D3 release ledger**：每次部署 append-only 記錄 commit、digest、分級、CI run、核准人與時間；
  runtime 從部署注入的 digest 回報身分，取代 D7 的 launch receipt。
- **D4 設定**：非秘密設定版本化在 repo，屬於 release 的一部分；秘密留在 VM，不進 image 或 CI。
- **D5 provenance**：先達 SLSA Build L1（CI 產出 build 紀錄＋digest）；cosign 簽章延到多租戶再上。

## Amendment（2026-09-25 獨立設計審查後）

- **D2**：「release manifest」實作為 GHCR `main` tag＋image label（revision、CI run）；VM 以 `docker buildx imagetools`
  解析 digest，部署工具的第三方依賴用 uv 釘住（Will：改用現成工具，不再自寫 registry client／YAML parser）。
- **D3**：ledger 兩階段 append（部署開始先寫 started，結束寫 outcome），bot 開機可讀到自己的部署列；CI run id 寫入 ledger。
- 主機端工具、systemd units 與 DR 腳本改由部署工具從目標版本套用，不再手動 `install.sh`、不再跑舊 working tree 的 DR 腳本。

## Rationale

- **選 C 而非 A**：Docker 官方建議在 CI build 並 pin digest；以 digest pull 就能保證 image 不變，
  自製 archive 驗證與 launch receipt 是在手工重做這個保證。筆電也不該在供應鏈上。
- **選 C 而非 B（推翻 09-22 D5'）**：12-Factor 要求 build 與 run 嚴格分離；SLSA 把同機 build 並執行
  列為 L0。今天 VM 上 34.75GB 的 build cache，就是 build 留在 production 主機的實際代價。
- **timer 拉取而非 CI 推送**：prod 不必把 SSH 或 Tailscale 憑證交給 CI，攻擊面比較小。
- **簽章延後**：單一 operator、單一主機下，cosign 的金鑰管理成本高於它能擋的威脅；GitHub artifact
  attestation 在 private repo 需要 Enterprise 方案。
- **代價**：GitHub 帳號安全成為供應鏈的前提（需開 2FA，push 權限只給 CI）；GHCR 儲存費用待查證；
  CI build 時間取決於 runner 規格。

## Expected Outcome

- 部署不經過筆電，一個 release 從 merge 到上線 <15 分鐘，rollback＝換回上一個 digest。
- 刪除自製 tar 驗證、launch receipt、`/opt/bfx/releases` 目錄結構與 VM 上 repo 外的部署腳本。

## Followup

- 查證 GHCR private image（約 1GB 壓縮後）的儲存與流量費用；確認 arm64 runner 的 build 時間。
- 建 CI workflow（build、push、release manifest）、VM 唯讀拉取憑證、VM 端部署 timer 與 rollback。
- release ledger 表與 runtime 身分回報；刪除 `image_artifact.py`、`release_package.py`、
  `immutable_release.py` 的 tar 路徑與 `release_identity` 的 launch receipt。
- 將待辦「release 改在 VM 上 build（D5'）」標為由本 ADR 取代。

## Invariants

- production 只執行 CI 從 `main` build、以 digest pin 的 image；VM 上不 build。
- 秘密不進 image、不進 CI。

## Revocation Triggers

- 開放外部資金或多租戶 → cosign 簽章＋部署端驗證，提升到 SLSA L2／L3。
- GitHub 帳號或 CI 遭入侵 → 重評信任邊界。

## Related

- 來源：2026-09-25 與 Will 討論，本 ADR 即原始紀錄。業界依據（當日實際查證）：Google SRE Workbook
  Ch.16（「Deployments should be performed by computers, not humans」）；12-Factor V《Build, release,
  run》；SLSA v1.0 Build Levels；Docker build best practices 與 `docker image pull` 的 digest 說明；
  GitHub Changelog 2026-01-29（private repo 開放 arm64 standard runner）；GitHub artifact attestations
  文件（private repo 需 Enterprise Cloud）。
- 修正對象：[2026-09-19-dynamic-capital-and-release-canary](2026-09-19-dynamic-capital-and-release-canary.md) D5、D5'（Amendment 2026-09-22）、D7。
- 同批：[2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md)。
- 實作：release workflow `1a56aee`、VM tooling `5030e5d`（merge `97c7612`），兩階段 ledger／DR 腳本取目標版本等 design-review 修正記於 plan：`2026-09-25-release-governance-refactor.md`（原文已不在 repo，本 ADR 即紀錄）。
