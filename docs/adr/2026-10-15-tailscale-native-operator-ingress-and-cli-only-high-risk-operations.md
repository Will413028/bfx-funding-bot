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
- **D4 高風險操作只走 CLI**：resume、啟用幣別、換交易所憑證、UNKNOWN 裁決只能由 host CLI 經 Tailscale SSH 執行，SSH policy 用 check mode 且每次連線都重新到 IdP 驗證（重新驗證的間隔已修訂為 12 小時，見 Amendment D4d）。CLI 寫同一個 operator-request outbox，actor 取 Tailscale SSH 的身分，不是自填字串。UI 只提供讀取與降低風險的操作（halt、停用幣別），手機不提供高風險操作。Tailscale 本身故障時的最後手段是 OCI Console Connection（serial console，不開 port），寫進 runbook 並演練。
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

## Amendment (2026-10-15)：D4 的 CLI 執行形狀、換憑證路徑與 SSH 入口

CLI 那一批開工前的設計審查（獨立 subagent），對照本 ADR 的目標架構後補上 D4a–D4c；D4d 是設定 SSH policy 時發現的方案限制。皆由 owner 拍板。

- **D4a CLI 的執行位置與 DB 身分**：CLI 以 one-off 容器執行（與 app 同一個映像、直接接資料網路，不經 tailscaled sidecar，也不進 webapi 的 process）。它用專屬的 DB role，只有各 outbox request 欄位的 INSERT 與需要的讀取。營運介面移除高風險操作之後，由 DB trigger 拒絕 webapi role 寫入 resume、啟用幣別與 UNKNOWN 裁決，讓「只能由 CLI 寫入」有資料層保證。請求的驗證與組裝抽成 application service，webapi 與 CLI 都只是轉接層。
- **D4b 換憑證**：照 [2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md) D5'，CLI 那一批就新增換憑證的 outbox（沿用既有 outbox 契約），由 bot 驗證新 key、停用舊的、啟用新的，再自行重啟載入。CLI 不持有 KEK。D5 的 KEK 搬進 OCI Vault 仍在交易所憑證那一批做。
- **D4c SSH 入口**：Tailscale SSH 與 OCI Console Connection 都演練過之後，停用一般 sshd，雲端防火牆不開 22 port；主機只剩 Tailscale SSH 一個 SSH 入口。
- **D4d check mode 的重新驗證間隔**：check mode 採 Tailscale 預設的 12 小時，不設成每次連線（`checkPeriod: always`）；補強是 operator 裝置的 node key 到期設短，以及 SSH policy 只允許非 root 使用者。
- **過渡**：營運入口切換（membership 改用 node user ID）之前，CLI 請求的 actor 欄位是設定的 operator id，SSH 身分記在主機日誌，CLI 請求也仍受既有 operator 授權條件約束；切換之後 actor 就是 SSH 身分。

**Rationale**

- **D4a 不用 `docker exec` 進 webapi**：營運入口切換後，webapi 共用 tailscaled sidecar 的 network namespace，部署全停（D6）後 schema 不符時也拒絕開機。break-glass 要備援的正是這些故障，exec 會跟著失效。業界做法是把管理工作當 one-off process（12-Factor App XII、`heroku run`、`kubectl run`）。**不沿用 webapi role**：webapi process 被攻破時，仍能插入 resume 請求並填上 operator id；每個寫入者一個 role，與 08-31 D4' 讓 webapi 與 bot 分 role 是同一個做法。**代價**：多一個 role、一個 runtime 秘密檔與一個 migration；role 要在 migration 之前先建好。
- **D4b 不讓 CLI 直接寫 vault**：直接寫會牴觸 D5'；而且現行的「建立新 key → 驗證 → 刪舊的」在「同時只能有一個 active」的規則下不會讓新 key 生效，交易所憑證那一批也要整段重寫。OCI Vault 只擋「搬 KEK」，不擋「由 bot 驗證與啟用」，所以沒有理由等到那一批。**代價**：CLI 那一批多一張 outbox 表與一個 worker。
- **D4c 不改成「sshd 只聽 tailnet 介面」**：持有 key 的人一樣能經 tailnet 連 sshd、不經 check mode，D4 的重新驗證就不成立。**代價**：SSH（包括 coding agent 的維運）要 owner 到 IdP 驗證；Tailscale 故障時只剩 Console Connection。
- **D4d 選 12 小時而非每次連線**：自訂 `checkPeriod` 只有 Tailscale 付費方案（Premium、Enterprise）提供，免費方案存檔時直接拒絕。另一個選項是由 CLI 在寫入前自己要求硬體金鑰或通知確認，但那等於把 D2 移除的 app 驗證加回來。單一 operator、owner 自有的小額資金，不值得為一條規則付月費。**代價**：驗證後 12 小時內，同一台裝置執行高風險操作不必再過 IdP；ASVS V7.5「敏感操作前重新驗證」以 12 小時的窗口記為部分符合。

## Amendment (2026-10-18)：D4a 的資料層保證（9c-2b）

D4a 寫「由 DB trigger 拒絕 webapi role 寫入 resume、啟用幣別與 UNKNOWN 裁決」。實作時讀碼後改為兩種機制（migration `36e59526f8cf`）：

- **D4a' 裁決以收權限擋**：`uncertainty_resolution_requests` 的每一列都是裁決（`action` 只有 bind_to_venue／mark_not_accepted／manual_resolution），webapi 不再需要任何 INSERT，直接 `REVOKE INSERT`，比 trigger 簡單也更強。換憑證同理：`bfx_webapi` 對 `exchange_account_credentials` 收回 INSERT／UPDATE（端點一併刪除）。
- **D4a'' resume 與啟用以允許清單 trigger 擋**：`trading_control_requests` 還要留 kill、`capital_policy_requests` 還要留停用，所以兩張表各掛一個 `BEFORE INSERT OR UPDATE OF action` trigger（`guard_operator_only_request_action`），`resume`／`enable` 只允許 table owner（migration、owner 腳本）與 `bfx_operator` 寫入。用允許清單而不是「拒絕 `bfx_webapi`」：任何其他持有請求欄位 INSERT 的 role（日後新增的也一樣）都被拒絕，不必記得逐一列入。`bfx_bot` 對兩張表本來就沒有 INSERT。
- **Rationale**：trigger 只用在同一張表必須保留部分 action 的地方；整張表都是高風險動作時，權限本身就是最小的機制。trigger 與缺權限的錯誤同為 SQLSTATE 42501，測試以 trigger 的訊息區分。**代價**：上一版 webapi 在部署窗口內的 resume／啟用／裁決／憑證寫入會回 500，那正是要移除的功能；kill、停用與各種讀取不受影響。

## Amendment (2026-10-18)：營運入口切換不再等 CLI 的 VM 演練

原 Followup 要求營運入口切換前，CLI 的 resume、啟用幣別、換憑證與 UNKNOWN 裁決各在 VM 上演練過一次。owner 拍板拿掉這個前置條件：營運入口切換照排程做，不等演練。

- **現況**：
  - resume 與 kill 已在 VM 上經 `bfx-op` 演練過，當時 webapi 是停著的。
  - 啟用幣別、換憑證與 UNKNOWN 裁決走同一條 CLI 路徑（`bfx-op` → one-off `operator` 容器 → outbox → bot 的 worker），由 integration 測試對真實 PostgreSQL 與 `bfx_operator` role 驗證。
  - 這三項沒有在 prod 實際跑過。
- **Rationale**：
  - 這項前置條件要防的是「入口切換後 operator 沒有能用的高風險操作路徑」。但入口切換只改 webapi 的入口與身分來源，CLI 不在它的路徑上：D4a 讓 CLI 以 one-off 容器直接接資料網路，不經 webapi、不經 tailscaled sidecar。所以切換成功或失敗，都不會改變 CLI 能不能用。
  - 在 prod 演練這三項都會改到真實狀態：要先停用一個幣別再啟用、要有一筆 UNKNOWN、要在 Bitfinex 開一把新 key。在單一 operator、小額資金的條件下，這個代價大於它的保證。
  - UI 的高風險路徑已在入口切換前移除，所以演練與否也不改變「入口切換時有幾條路徑」。
- **代價**：這三項第一次在 prod 使用時才算真正驗過。第一次使用就照 runbook 操作並記錄結果。若失敗，修好之前，該操作沒有其他路徑。
- **Revocation trigger**：網路分段那一批若會改到 `operator` 容器接的網路，或 CLI 改成經過 webapi 或 sidecar，該批完成前要先演練受影響的操作。

## Amendment (2026-10-20)：D1 的授權改看 app capability，webapi 在 tailnet 節點的 namespace 裡（9d-5）

D1 寫「身分取自 Serve 注入的身分 header，穩定鍵是 node user ID」。9d-0 spike 實測後改成下面的形狀，9d-5（PR #237）實作並部署：

- **D1a 授權看 app capability**：tailnet policy 的 grant 把 capability `github.com/Will413028/bfx-funding-bot/cap/operator` 授予 operator 對 `tag:bfx-ops` 的連線，值是 `{"user_id": "<operator 的 node user ID>"}`；serve.json 的 `AcceptAppCaps` 讓 Serve 以 `Tailscale-App-Capabilities` header 轉送。webapi 只在這個 capability 裡有一筆 `user_id` 等於 `BFX_OPERATOR_TAILNET_USER_ID` 時放行，否則 401；`Tailscale-User-Login` 只拿來顯示。Serve 會覆寫 client 自己送的 `Tailscale-*` header（spike 實測）。
- **D1b webapi 只聽 tailnet 節點 namespace 裡的 127.0.0.1**：webapi 以 `network_mode: service:netns` 加入 holder 的 network namespace（計畫 D13），與 tailscale 共用，所以 Serve 是唯一入口；前端 static export 由同一個 webapi 同源提供。主機的 443 Funnel 已在 2026-10-20 關閉。
- **Rationale**：capability 而非 login header：授權條件寫在 tailnet policy 裡，換裝置或 login 改名都不必動 app，policy 拿掉 grant 就立即撤權；node user ID 帶在 capability 的值裡，app 不用另外解析身分 header 的格式。不選「只比對 `Tailscale-User-Login`」：login 字串可改名，而且任何能連到節點的 tailnet 成員都會帶這個 header，等於把授權交給 ACL 一層。**代價**：policy 的 grant 值與 webapi 設定必須一致，任一邊改錯 UI 全部 401（部署前以 `tailscale whois` 核對）；capability 需要 tailscale ≥ v1.92。
- **Revocation trigger**：Tailscale 不再轉送 app capability，或 Serve 不再覆寫 client 送的 `Tailscale-*` header → 退回 D1 原文的身分 header 比對，並重新評估 Revocation Triggers 裡的 identity-aware proxy。

## Amendment (2026-10-24)：tailnet 身分的實際強度與 session 時效（ASVS V6.8.4、V7.1、V7.3）

ASVS 對照把 V6.1.3、V6.8.4、V7.1.1–V7.1.3、V7.3.1、V7.3.2 判為不符合，缺的是實際值與決定。2026-10-24 的值：GitHub 帳號有開兩步驟驗證，實際的因子由 maintainer 記在私有的威脅模型，不寫進 public repo；tailnet 的登入只有 GitHub（`tailscale status --json` 的 login 是 `…@github`）；operator 裝置 2026-05-31 06:11:49 UTC 登錄、node key 2026-11-27 06:11:49 UTC 到期，正好 180 天，也就是 tailnet 的 key expiry 是 Tailscale 預設的 180 天、沒改過（`tailscale status --json` 的 `Self.Created`、`Self.KeyExpiry`）；OCI 帳號有 MFA（Will）。各路徑的強度與時效寫在 `backend/ARCHITECTURE.md` §10h。

- **D1c 最低強度假設**：app 收不到也不驗 `acr`、`amr`、`auth_time`（Serve 只轉送 login 與 capability）。app 一律假設最低強度：持有已登錄的 operator 裝置，加上一個可被釣魚的 IdP 因子。因此 UI 只開讀取與降低曝險的寫入（D4、§10a「動作的分界」），會擴大曝險的操作要經 SSH check mode 再回 IdP。D1「IdP 只用硬體金鑰」是否達成，記在私有的威脅模型；它是 owner 在帳號後台的設定，repo 裡沒有可以強制它的地方，所以本假設不以它為前提。
- **D1d node key 到期維持 180 天**：Will 2026-10-24 決定先不縮短，修訂 D1「operator 裝置的 node key 到期設短」。UI 的 session 上限就是這 180 天。
- **D2a 不設 idle 時限**：Tailscale node key 只有 absolute 到期、沒有 idle 時限；app 層的 idle 要把 D2 移除的 app session 加回來。閒置的風險由 operator 裝置的螢幕鎖承擔。

**Rationale**

- **D1c 不在 app 層補驗證強度**：拿不到 IdP 的 claim，能做的只有在 app 裡再加一道自己的驗證，那等於把 D2 移除的登入加回來。動作分界已經讓最低強度下的 UI 只能讀取、kill 與停用幣別，最壞的結果是停止放貸，不是資金移動。**代價**：非硬體金鑰的因子可被即時釣魚；被釣到時攻擊者能登錄新裝置並使用 UI，也能在 12 小時的 check 窗口內用 CLI。
- **D1d 不縮短**：縮短只影響 UI（CLI 每 12 小時本來就回 IdP），而 UI 的寫入限於降低曝險。**代價**：裝置遺失或被盜時，到移除裝置之前 UI 都可用；所以裝置遺失時的第一步是在 admin console 移除它（§10h 的終止步驟）。
- **D2a 不選 app 層 idle**：理由同 D1c。**代價**：螢幕未鎖的裝置，閒置多久都能用 UI。

**Revocation trigger**：UI 新增任何會擴大曝險的寫入、開放第二個使用者（08-31 SP6）、Tailscale 提供 idle 或裝置狀態控制，或 IdP 帳號的驗證因子改變（屆時重評 D1d 的天數）。

## Followup

- 依對真錢的風險分批實作，每批一個 PR：公開頁靜態化 → CLI 對等與 break-glass 演練 → 營運入口切換 → 部署全停 → 網路分段 → KEK 與交易所憑證 → 秘密管理、供應鏈、安全 log、API 契約與文件。
- 交易所憑證那一批同時讓執行服務在開機與定期檢查憑證的權限與來源限制，不合規就停止送單、恢復後自動繼續；該批另以 ADR 記錄。
- 營運入口切換前，CLI 必須已能完成 resume、啟用幣別、換憑證與 UNKNOWN 裁決；VM 演練的要求已由 Amendment (2026-10-18)「營運入口切換不再等 CLI 的 VM 演練」移除。
- ASVS L2 對照在全部實作完成後重新判定，放進 `docs/security/`（只寫判定與控制）。

## Revocation Triggers

- tailnet 改用付費方案，或開始保管外部使用者的資金 → D4d 改為 `checkPeriod: always`。
- 開放外部客戶（08-31 D7 的 gate 通過）→ 另建客戶 app 與它自己的登入；營運介面維持本 ADR。
- 需要多實例或有可用性 SLO → D6 改回 expand/contract。
- Tailscale 的條款、價格或可用性不再適合 → 換成 identity-aware proxy（選項 B 或 Pomerium／oauth2-proxy），app 只換身分來源。
- 開始保管外部使用者的交易所憑證 → 重評 KEK 的隔離層級（HSM、每帳戶一把 key）。

## Related

- 本 ADR 即原始紀錄：2026-10-15 owner 與 coding agent 的討論；方案草稿經三輪獨立 subagent 批判後由 owner 拍板。業界依據：NIST SP 800-207（zero trust）、NIST SP 800-63B-4（phishing-resistant 驗證）、OWASP ASVS 5.0（V3.5 跨站請求、V6.8 身分 assertion、V7.5 重新驗證）、OWASP Secrets Management Cheat Sheet、Tailscale Serve／SSH 文件。
- 取代 [2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap](2026-09-20-operator-totp-enrollment-and-host-only-admin-bootstrap.md)（整份）。
- 部分取代 [2026-06-06-frontend-saas-architecture](2026-06-06-frontend-saas-architecture.md)、[2026-06-07-web-tier-hosting-funnel-scoped-roles](2026-06-07-web-tier-hosting-funnel-scoped-roles.md)；修訂 [2026-06-07-sp2-bfx-key-vault](2026-06-07-sp2-bfx-key-vault.md)、[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)、[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)、[2026-10-08-forward-only-migrations](2026-10-08-forward-only-migrations.md)。各 ADR 內有對應的註記。
