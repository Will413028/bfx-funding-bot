---
title: tailnet 節點與 webapi 共用的 network namespace 由只跑 sleep 的 holder 容器持有
date: 2026-10-20
status: active
tags: [bfx-funding-bot, decision, deploy, networking, tailscale]
related-commits:
  - "59d7a144^..6a496751"
  - 7fd24349
---

# network namespace 由 holder 容器持有

## Context

[2026-10-15](2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md) 讓 webapi 只聽 `127.0.0.1`，唯一入口是 tailnet 節點的 Tailscale Serve；webapi 用 `network_mode: service:<x>` 加入另一個容器的 network namespace（以下簡稱 netns）。netns 的持有者一旦重啟或重建，Docker 會給它新的 netns，加入者留在已死的那個，Docker 不會自己修。最初的設計是 tailscale 容器持有 netns，部署工具每一輪檢查並重建卡住的加入者（`_repair_netns`，3 次上限後告警）。開工前的設計審查指出：持有者正是最常重啟的 tailscale。

**約束**：

- `external` Docker 的 `network_mode: service:` 語意：持有者換 netns 時加入者不跟著換。
- `inherited` 不新增依賴（`AGENTS.md`「供應鏈與依賴」）：仍成立；已核准的 tailscale image 可以重用。
- `inherited` 部署不改變交易狀態：修復只能重建加入者，不碰 bot。

## Options Considered

- **基準. 專用的 infra 容器持有共享 netns，工作容器只加入**：Kubernetes pod 的 pause（infra）container，應用容器重啟不影響 pod 的網路（Kubernetes 文件 "Pods"、`pause` image）。
- **A. tailscale 持有 netns，部署工具每一輪修復**：沿用 2026-10-20 已做的 tick 修復。
- **B. 加一個只跑 `sleep` 的 holder 容器持有 netns，tailscale 與 webapi 都加入它（採用）**：沿用已核准的 tailscale image，只接 `bfx_default`。

## Decision

- **D1 = 計畫 D13**：選 B。holder 服務名 `netns`、容器 `bfx-netns`。
- **D2**：部署時對加入者的 netns 檢查保留。tick 修復只在「holder 原地重啟有人看得到」時才刪：tailscale healthcheck 轉 unhealthy 要有告警。這個告警目前不存在，所以修復保留，目標改成 holder，修所有加入者（PR #236）。
- **D3**：runbook 寫明不得單獨 `docker restart bfx-netns`。

## Rationale

- **D1**：A 讓最常重啟的元件持有共享資源，每次 tailscale 重啟、崩潰或升級都要靠修復收尾；B 照 Kubernetes 的做法讓持有者幾乎不動，問題從「常發生、靠修」變成「很少發生」。**代價**：多一個容器；holder 自己原地重啟時加入者仍會卡住（同一類問題，機率低）。
- **D2**：刪修復的前提是剩下那一個觸發有人看得到；沒有告警時刪掉，等於把低機率事件變成沒人知道的停機。保留修復的**代價**是部署工具多一段要維護的程式。

## Result

- `git log --oneline 59d7a144^..6a496751`（PR #235：holder 與 tailnet 節點）；`7fd24349`（PR #236：修所有加入者）。
- holder spike 實測：tailscale 重啟、崩潰、單獨重建都不換 netns；開機 6／6 次順序正確；holder 原地重啟仍會留下死 netns。
- 2026-10-20 起正式環境運作；如預期。

## Invariants

- 加入共享 netns 的服務與 holder 在同一次 `compose up`；部署檢查每個加入者在活的 netns 裡。
- holder 只持有 netns，不跑任何服務；它的 image 與 tailscale 相同。

## Revocation Triggers

- homelab 觀測加上 tailscale healthcheck unhealthy 的告警 → 刪 tick 修復（`_repair_netns`、`netns-repair.json`），只留部署時的檢查。
- 入口改成不需要共享 netns 的形狀（例如 tailscaled 在主機上）→ 拿掉 holder。

## Related

- 壓縮自 gitignored 的品質基線 Prompt 3 計畫（2026-10-12 起）的 D13 與當時的 holder spike 紀錄；該目錄從未進版控，內容僅存本 ADR。
- `docs/runbooks/deploy.md`「Tailnet 入口」與「加入者留在 holder 已死的 network namespace」；`backend/ARCHITECTURE.md` 部署段。
