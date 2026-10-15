---
title: 營運入口改為 Tailscale 原生並移除 app 登入；公開頁改為 Cloudflare Pages 靜態檔；KEK 移進 OCI Vault；高風險操作只走 CLI；部署改為全停
date: 2026-10-15
status: active
supersedes: "[2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap](2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap.md)；[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md) 的 D3、D4 的 `auth` schema、D6 的 TOTP／step-up、Amendment D7、D8 與 D9 的身分部分；[2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md) 的 D2、D3 的 `bfx_webauth`、D4、D5、D6；[2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md) 的 D5；[2026-10-06-switch-by-seed-and-retire-the-legacy-authority](2026-10-06-switch-by-seed-and-retire-the-legacy-authority.md) 的 D6"
amends:
  - "[2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md) D1"
  - "[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D4'、D5 與 Followup 的 expand/contract"
  - "[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md) D2"
  - "[2026-10-08-forward-only-migrations](2026-10-08-forward-only-migrations.md) 的部署約束"
tags: [bfx-funding-bot, decision, security, auth, deployment, architecture, frontend]
---

# 營運入口 Tailscale 原生、高風險操作只走 CLI、部署全停

## Context

2026-10-15 依 OWASP ASVS 5.0 L2 與威脅模型做了一次安全審查；結果由 maintainer 私下保存，修補完成前不公開。原本整理成逐項補丁，owner 要求對照業界做法、改用目標架構一次解決，並放寬兩個前提：bot 只有 owner 自己的小額資金，web 與登入層沒有外部使用者，可以直接做破壞性改動、不留相容窗口；交易路徑的改動仍要有回滾。

審查中有幾項是實作沒跟上既有決定：[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D6 規定公開頁用預先產生的 static artifact，D5 規定外部 key 之前要有 KMS 多版本輪替，兩者都還沒實作。

**約束**：

- `external` 沒有自有網域（[2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md)）。Tailscale 同一個 port 只能 Serve 或 Funnel 二選一，Funnel 的請求不帶身分 header。
- `external` 主機在 OCI；單機、單一 operator。
- `inherited` 產品邊界（bridge model、開放外部客戶前的 gate）由 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D2、D7 決定：仍成立，本 ADR 不重開。
- `inherited` 帳戶隔離核心（`ExchangeAccount`、operator-request outbox、worker lease、writer lock）、command gate、ledger 與 venue 歷史：仍成立，本 ADR 不動。
- `inherited` 放貸全自動（`AGENTS.md`「營運原則」）：仍成立；營運入口故障時，resume 與 UNKNOWN 裁決不能只剩一條路。
- `inherited` forward-only migration、migration 自帶資料斷言、部署前的 restore gate：仍成立。

## Options Considered

**營運入口與身分**

- **基準. identity-aware proxy**：管理介面放在 zero-trust access proxy 後面（BeyondCorp／Google IAP、Cloudflare Access、Pomerium、oauth2-proxy；NIST SP 800-207），登入與 phishing-resistant MFA 由 IdP 或 proxy 負責，app 只驗 proxy 給的身分。客戶用的登入是另一個 app。
- **A. 維持 Better Auth 並逐項補強**（passkey、session 時效、認證強度帶進 token）：app 仍自己維運一套登入。
- **B. 買網域，Cloudflare Tunnel＋Access**（Q1-a）：最接近基準；多一個網域與廠商，Cloudflare 同時管公開站、營運入口、身分與異地備份。
- **C. Tailscale Serve＋只留 passkey 的最小 Better Auth**（Q1-b）：不需網域，但保留一套 app 登入。
- **D. Tailscale 原生**（Q1-c，採用）：營運介面只在 tailnet 內，身分取自 Tailscale Serve 注入的 header，app 沒有自己的登入。

**公開頁**：A. Cloudflare Pages（採用，`*.pages.dev` 免網域）；B. GitHub Pages（VM 要持有能推 repo 的憑證）；C. 買網域後放 R2。

**KEK 存放**：基準是 KMS／HSM envelope encryption（KEK 不出 KMS、可輪替、解密有稽核）。A. OCI Vault（採用）；B. 自托 OpenBao Transit；C. 維持 env KEK。

**高風險操作（resume、啟用幣別、換交易所憑證、UNKNOWN 裁決）**：A. UI 加重新驗證（step-up）；B. 只走 host CLI，經 Tailscale SSH check mode（採用）。

**部署**：基準（有可用性 SLO、多實例的系統）是 expand/contract、零停機；A. 維持只停 bot、webapi 跑上一版；B. 有 migration 時全停（採用）。

## Decision

- **D1 營運入口**：營運介面只經 Tailscale Serve 提供（tailnet 內、只有 HTTPS），Funnel 關閉，VM 上不再有任何公開入口。
  - 身分取自 Serve 注入的身分 header，穩定鍵是 Tailscale node user ID，不用可改名的 login 字串；沒有身分或不是設定的 operator 一律拒絕。tailnet ACL 只讓 operator 的裝置連到這個服務。
  - tailscaled 以 sidecar 容器執行，webapi 共用它的 network namespace、只聽 loopback，只有 sidecar 接到資料網路；其他容器與 host 上的程序都連不到 webapi。compose policy 強制這個形狀。
  - 所有非 GET 請求都要通過跨站請求檢查（同源的 `Sec-Fetch-Site` 或 `Origin`、JSON content type、`Host` allowlist），不開 CORS。
  - 認證強度由 tailnet 的 IdP 帳號負責，只用硬體金鑰；operator 裝置的 node key 到期設短，VM 是 tagged node、不設到期。
- **D2 移除 app 登入**：移除 Better Auth、TOTP、MFA marker、Next.js BFF route handler、JWT／JWKS、`auth` schema 與 `bfx_webauth` role、Redis。前端改為 static export，由 webapi 同源提供並加上安全 header。membership（owner／operator／viewer）保留，鍵改為 node user ID；`Principal` 介面保留，日後客戶 app 的 OIDC 驗證可以接上。
- **D3 公開頁**：proof summary 與 funding-rates CSV 由 VM 上的排程工作產生 static 檔，部署到 Cloudflare Pages；webapi 移除公開端點。這是 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D6 的實作。
- **D4 高風險操作只走 CLI**：resume、啟用幣別、換交易所憑證、UNKNOWN 裁決只能由 host CLI 經 Tailscale SSH 執行，SSH policy 用 check mode 且每次連線都重新到 IdP 驗證。CLI 寫同一個 operator-request outbox，actor 取 Tailscale SSH 的身分，不是自填字串。UI 只提供讀取與降低風險的操作（halt、停用幣別），手機不提供高風險操作。Tailscale 本身故障時的最後手段是 OCI Console Connection（serial console，不開 port），寫進 runbook 並演練。
- **D5 KEK 移進 OCI Vault**：交易所憑證維持 envelope encryption，KEK 改由 OCI Vault 保管，支援多版本與 re-wrap。webapi 不持有 KEK、不呼叫 Bitfinex；憑證驗證經 outbox 由 bot 執行。
- **D6 部署全停**：有 migration 時 bot 與 webapi 都停，migrate 完再啟動；webapi 與 bot 一樣，schema 不是預期的 head 就拒絕開機。schema 變更不再需要對上一版 webapi 向前相容（expand/contract）。

## Rationale

- **D1 選 D 而非 B**：B 需要網域與新廠商，且把公開站、營運入口、身分與備份集中在同一個帳號；VM 已經在用 Tailscale，D 不增加外部依賴。**不選 A、C**：只要 app 自己有登入，密碼、MFA、session、復原流程都要單人維運，攻擊面也留在 app 裡；營運介面只有一個 operator，不需要 app 層的帳號系統。**代價**：app 看不到 MFA 方式，認證強度只能以 IdP 的設定證據記錄；Tailscale 的身分跟著連線走、不帶 cookie，SameSite 擋不了跨站請求，所以跨站請求檢查是必要條件而不是選配；Tailscale 成為入口的單點，由 D4 的 break-glass 補上。
- **D2**：[2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md) 把「翻 flag 即客戶 SaaS」的登入放進營運介面，但 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D7 已把開放客戶綁在多個 gate 上，客戶 app 與營運介面也本來就該分開部署。app 沒有 session 之後，BFF 與 Redis 都沒有存在的理由，少一個 Node runtime 與一個資料服務。
- **D3 選 A**：公開流量不再進 money VM；B 要讓 VM 持有能推 repo 的憑證，C 要先買網域。**代價**：VM 持有一把只能部署該 Pages 專案的 token；資料有批次延遲（08-31 D6 已接受）。
- **D4 選 B 而非 A**：ASVS V7.5 要求敏感操作前重新驗證；app 沒有登入之後，Tailscale SSH check mode 就是這一步，而且每次連線都要過 IdP。CLI 與 UI 寫同一個 outbox，入口故障時放貸仍能恢復，符合「放貸全自動」。啟用幣別和 resume 一樣會擴大交易，所以歸在同一類；停用只降低風險，留在 UI。**代價**：resume 與啟用幣別要用有 SSH 的裝置，手機做不到。
- **D5 選 A 而非 B**：主機本來就在 OCI，software key 免費，不多一個要維運的服務；envelope 設計讓日後換 KMS 不必搬資料。不選 C：env KEK 無法輪替、解密沒有稽核。webapi 不持有 KEK，金鑰只剩執行服務一個使用者。**代價**：KMS 擋的是資料庫與設定檔的離線外洩，並提供輪替與稽核；擋不住主機本身被完整控制。
- **D6 選 B**：expand/contract 與零停機是給多實例、有可用性 SLO 的系統；這裡是單實例，funding offer 掛在交易所上，停幾分鐘對錢沒有影響。全停之後，「上一版 webapi 碰到新 schema」不再發生，不必另寫相容性測試。**代價**：每次 migration 時營運介面短暫不可用。

## Expected Outcome

- VM 沒有公開入口；營運介面只有 tailnet 內的 operator 裝置連得到，app 裡沒有密碼、TOTP 或 session。
- 公開頁的請求不經過 VM。
- resume、啟用幣別、換憑證、UNKNOWN 裁決在營運介面故障時仍能由 CLI 完成，紀錄的 actor 是 SSH 身分。
- KEK 不在任何容器的環境變數裡，可以輪替。
- 部署工具在 migration 時停掉所有讀寫 schema 的服務。

## Followup

- 依對真錢的風險分批實作，每批一個 PR：公開頁靜態化 → CLI 對等與 break-glass 演練 → 營運入口切換（CLI 演練完成後才做）→ 部署全停 → 網路分段 → KEK 與交易所憑證 → 秘密管理、供應鏈、安全 log、API 契約與文件。
- 交易所憑證那一批同時讓執行服務在開機與定期檢查憑證的權限與來源限制，不合規就停止送單、恢復後自動繼續；該批另以 ADR 記錄。
- 營運入口切換前，CLI 必須已能完成 resume、啟用幣別、換憑證與 UNKNOWN 裁決，並在 VM 上演練過一次。
- ASVS L2 對照在全部實作完成後重新判定，放進 `docs/security/`（只寫判定與控制）。

## Revocation Triggers

- 開放外部客戶（08-31 D7 的 gate 通過）→ 另建客戶 app 與它自己的登入；營運介面維持本 ADR。
- 需要多實例或有可用性 SLO → D6 改回 expand/contract。
- Tailscale 的條款、價格或可用性不再適合 → 換成 identity-aware proxy（選項 B 或 Pomerium／oauth2-proxy），app 只換身分來源。
- 開始保管外部使用者的交易所憑證 → 重評 KEK 的隔離層級（HSM、每帳戶一把 key）。

## Related

- 本 ADR 即原始紀錄：2026-10-15 owner 與 coding agent 的討論；方案草稿經三輪獨立 subagent 批判後由 owner 拍板。業界依據：NIST SP 800-207（zero trust）、NIST SP 800-63B-4（phishing-resistant 驗證）、OWASP ASVS 5.0（V3.5 跨站請求、V6.8 身分 assertion、V7.5 重新驗證）、OWASP Secrets Management Cheat Sheet、Tailscale Serve／SSH 文件。
- 取代 [2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap](2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap.md)（整份）。
- 部分取代 [2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)、[2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md)；修訂 [2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md)、[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)、[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)、[2026-10-08-forward-only-migrations](2026-10-08-forward-only-migrations.md)。各 ADR 內有對應的註記。
