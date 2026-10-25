# OWASP ASVS 5.0 Level 2 對照

本文件逐條判定本系統是否符合 OWASP ASVS 5.0.0 的 Level 1 與 Level 2 要求，並附上判定的依據。

- **條文來源**：OWASP ASVS release `v5.0.0_release` 的 `OWASP_Application_Security_Verification_Standard_5.0.0_en.csv`（`gh release download v5.0.0_release -R OWASP/ASVS`），取 L1 與 L2 共 253 條（全文 345 條，其餘 92 條是 L3，不在本文件範圍）。
- **範圍**：目前的整個系統：前端（Next.js static export）、webapi、bot，以及 `deploy/` 下的部署設定。營運介面只有一個 operator。
- **快照**：commit `9ddd941f`（`main`），2026-10-24。文中的行號都指這個 commit。
- **方法**：每一條都對照當時的程式碼、設定與文件判定，附上可以回查的依據（路徑與行號、測試、指令或 PR）。函式庫的預設行為以 lockfile 鎖定版本的原始碼為依據。
- **判定的意思**：
  - **符合**：附上依據（`path:line`、`file::test`、指令或文件章節）。
  - **不符合**：本文件中的「不符合」全部是**已接受的缺口**：控制不完整，但有記錄下來的理由、補償控制與重新評估的觸發條件，列在[已接受的缺口](#已接受的缺口)。
  - **不適用**：附上理由。以「只有一個 operator」當理由，只用在條文本身是多使用者行為的地方。
- 安全問題的回報方式見 [`SECURITY.md`](../../SECURITY.md)。

### 路徑縮寫

- `core/…`、`modules/…`、`apps/…`、`external/…` 都在 `backend/src/bfx_funding_bot/` 下；`tests/…` 在 `backend/tests/` 下。
- `ingress.py` = `modules/api/ingress.py`；`auth.py` = `core/auth.py`；`schemas.py` = `modules/api/schemas.py`；`trading_control.py` = `modules/api/trading_control.py`；`router.py` = `modules/admin/router.py`。
- `compose` = `deploy/vm/docker-compose.app.yml`；`serve.json` = `deploy/vm/tailscale/serve.json`；`bfx_op.py` = `deploy/vm/ops/bfx_op.py`；`compose_policy.py` = `deploy/vm/ops/compose_policy.py`。
- `operations.md`、`fresh-host-setup.md`、`secret-rotation.md`、`deploy.md`、`offsite-dr.md` 都在 `docs/runbooks/` 下；`ARCHITECTURE.md` = `backend/ARCHITECTURE.md`。
- `ADR-1015` = `docs/adr/2026-10-15-tailscale-native-operator-ingress-and-cli-only-high-risk-operations.md`；`ADR-0831` = `docs/adr/2026-08-31-account-isolated-execution-target-architecture.md`。
- 函式庫版本（`backend/uv.lock`）：starlette 1.7.0、httpx 0.28.1、httptools 0.7.1、websockets 16.0；前端 next 16.4.1、next-intl 4.14.x。

## 總覽

共 253 條：符合 130、不符合 17（全部已接受）、不適用 106、待證 0。

| 章 | 名稱 | 符合 | 不符合 | 不適用 | 待證 |
|---|---|---|---|---|---|
| V1 | Encoding and Sanitization | 14 | 0 | 13 | 0 |
| V2 | Validation and Business Logic | 11 | 0 | 0 | 0 |
| V3 | Web Frontend Security | 17 | 0 | 2 | 0 |
| V4 | API and Web Service | 5 | 0 | 5 | 0 |
| V5 | File Handling | 3 | 0 | 6 | 0 |
| V6 | Authentication | 6 | 0 | 29 | 0 |
| V7 | Session Management | 11 | 2 | 5 | 0 |
| V8 | Authorization | 6 | 0 | 1 | 0 |
| V9 | Self-contained Tokens | 0 | 0 | 7 | 0 |
| V10 | OAuth and OIDC | 0 | 0 | 29 | 0 |
| V11 | Cryptography | 10 | 3 | 1 | 0 |
| V12 | Secure Communication | 3 | 5 | 1 | 0 |
| V13 | Configuration | 8 | 5 | 0 | 0 |
| V14 | Data Protection | 9 | 0 | 0 | 0 |
| V15 | Secure Coding and Architecture | 13 | 0 | 0 | 0 |
| V16 | Security Logging and Error Handling | 14 | 2 | 0 | 0 |
| V17 | WebRTC | 0 | 0 | 7 | 0 |
| **合計** | | **130** | **17** | **106** | **0** |

## 驗證路徑

app 本身沒有登入、密碼、MFA 或 session（見[不存在性證據](#不存在性證據) E5）。使用者驗證交給 tailnet：IdP 是 GitHub 帳號，裝置以 IdP 登入 tailnet 後取得 node key。各路徑的強度與時效集中在 `ARCHITECTURE.md` §10h，決定與理由在 ADR-1015 的 Amendment (2026-10-24)。

- **(a) 營運 UI**：tailnet 內的 operator 裝置 → `bfx-tailscale` 節點（`tag:bfx-ops`）的 Tailscale Serve（`serve.json`：443 → `127.0.0.1:8000`，`AcceptAppCaps` 只轉送 operator capability）→ webapi。
  - webapi 只聽同一個 network namespace 的 loopback（`compose:109,143`），這個形狀由 `compose_policy.py`（`WEBAPI_NETWORK_MODE` 與 webapi 的 command）在 CI 檢查。Serve 會覆寫 client 自己送的 `Tailscale-*` header（ADR-1015 Amendment (2026-10-20)，`:123`）。
  - `require_operator`（`auth.py:66-81`）要求 `Tailscale-App-Capabilities` 帶 operator capability，且其 `user_id` 等於設定的 `BFX_OPERATOR_TAILNET_USER_ID`（`hmac.compare_digest`，`:74-75`），否則回 401 並記 `access_denied`。之後由 membership 決定能碰哪個帳戶、能不能寫（`modules/api/account_scope.py:48-76`）。
  - 誰拿得到 capability 由 tailnet policy 的 grant 決定（`fresh-host-setup.md:215-219`；實際內容在 Tailscale admin console）。
  - UI 只能讀取、kill、停用幣別與改 config draft。
- **(b) CLI**：Tailscale SSH 進主機（check mode，12 小時內同一台裝置不再回 IdP 驗證；`operations.md:708-710`），再 `sudo bfx-op <子命令>`。
  - `SSH_CONNECTION` 的 peer 必須是 tailnet 位址，身分由 `tailscale whois` 取得（`bfx_op.py:182-216`）；CLI 確認這個 ID 是該帳戶的 member（`apps/operator_cli.py:277-284`），寫入另要 owner／operator，bot 套用前再以 `operator_authorized` 重驗。
  - 高風險操作（resume、啟用幣別、UNKNOWN 裁決、換憑證）只有這條路（ADR-1015「高風險操作只走 CLI」），DB trigger 擋住 webapi 寫入（`ARCHITECTURE.md:804-807`）。
- **(c) 緊急停機**：`sudo bfx-op halt` 不要求 tailnet 身分。它在 bot 容器內以 `BFX_ADMIN_TOKEN` 呼叫 `/admin/halt`，只能降低曝險。
- **(d) break-glass**：OCI Console Connection，不經 tailnet（OCI 帳號的密碼＋MFA，`ARCHITECTURE.md` §10h）。Console 下 `bfx-op` 除了 `halt` 一律回 `not_tailnet_peer`（`operations.md:788`）；取得的是主機 root，信任邊界寫在 `bfx_op.py:44-50` 與 `operations.md:716-718`。
- 公網 sshd 已停用（`ssh.socket`、`ssh.service` masked），UFW 與 OCI security list 都不開 22（`operations.md:767-768`；`ARCHITECTURE.md` §10a）。
- app 假設的最低強度：持有已登錄的 operator 裝置，加上一個可被釣魚的 IdP 因子（`ARCHITECTURE.md` §10h「最低強度假設」）。所以 UI 只開讀取與降低曝險的寫入，擴大曝險的操作要經 SSH check mode 再回 IdP。
- 不屬於使用者驗證、不在 V6 判定的：bot `/admin` 的兩把服務間 token（`router.py:34-46`）。

## 已接受的缺口

17 條「不符合」都是已接受的。下面每一條寫出：缺的控制、接受的理由、補償控制，以及什麼情況要重新評估。

### 單一 VM 的扁平網路與「主機等同 root」

適用：V12.3.1、V12.3.3、V12.3.4、V13.2.1、V13.2.2、V13.2.4、V13.2.5。

公開紀錄：`ARCHITECTURE.md` §10a「已接受的殘餘風險」、§10b 通訊矩陣與其後的說明。

- 共同理由：所有服務跑在同一台 VM、同一個 Docker 網路（`bfx_default`）。網路分段、服務間 TLS、egress allowlist 與第二個 Postgres 在單一 VM 上的管理成本高；最小權限改由每個工作自己的 DB role 與分權的 token 承擔。
- 共同的補償控制：
  - 這個網路沒有對外開放的 port；營運入口只有 tailnet 內的 Tailscale Serve。
  - webapi 只聽 tailnet 節點 namespace 裡的 loopback。
  - DB role 依服務分開（`fresh-host-setup.md` §1）。
  - webapi 只持有只讀的 bot token，持有 halt token 或 KEK 時拒絕開機（`apps/webapi.py:46-56`）。
- 共同的重新評估觸發：服務拆到一台以上的主機，或開放第二個使用者。

| 編號 | 缺的控制 |
|---|---|
| V12.3.1 | 對外連線都是 TLS，但 VM 內部的連線是明文：webapi→bot（`http://bfx-bot:8080`，`modules/api/funding_status.py:123`）、各服務→Postgres（`DATABASE_URL` 不帶 sslmode，預設 `disable`：`core/settings.py:51`；§10b），以及 OTLP tracing（預設 `http://tempo:4317`，`modules/observability/tracing.py:49`）。 |
| V12.3.3 | webapi 呼叫 bot `/admin/trading-status`、`/admin/dry-evaluate` 時走明文 HTTP，帶的是只讀 token（`modules/api/funding_status.py:117-126`）。 |
| V12.3.4 | 內部沒有 TLS，所以沒有內部 CA 或憑證 pinning。程式已支援 Postgres 的 `verify-full`＋`sslrootcert`（`core/db.py:72-87`），但部署沒有啟用。 |
| V13.2.1 | 服務之間的憑證（DB 密碼、bot 的只讀 token）是長期憑證，事件觸發才換（`secret-rotation.md`「何時換」），不是條文要求的短效 token 或憑證。 |
| V13.2.2 | 網路沒有分段：bot、webapi 與 tailscale 共用的 namespace、postgres 都在 `bfx_default`（`compose:100-101,174-175,244-247`），同網路的容器連得到 bot 8080 的 `/healthz`、`/readyz`、`/metrics`（不需認證；`apps/bot.py:1003-1004`、`modules/marketfeed/healthz.py:72-118`）。`/admin` 仍要 token。 |
| V13.2.4 | 應用程式的對外目的地都是常數或部署設定（§10b），但沒有在網路層強制的 egress allowlist。 |
| V13.2.5 | 同 V13.2.4：容器沒有可強制的對外請求 allowlist，只靠程式碼裡的常數。 |

### 金鑰保管與輪替

適用：V11.1.1、V11.2.2、V13.3.1。

公開紀錄：ADR-0831 Amendment (2026-10-22)「KEK 不上 KMS，改為秘密檔」；`secret-rotation.md`「何時換」「秘密的持有者」「Vault KEK」。

- 共同理由：目前系統裡只有 owner 自己的一把 Bitfinex key，秘密也只有 owner 一人持有。KMS 擋不住 VM 被 root 入侵，因為 instance principal 照樣能解密，明文 secret 本來就在 bot 的記憶體裡；它擋的是 boot volume 或備份外洩，並提供解密紀錄。代價是 bot 每次開機都依賴 OCI KMS 與 metadata service，還要多一個 SDK 依賴與一組雲端權限設定。
- 共同的補償控制：
  - 常駐秘密是 Compose file secrets，`root:4600 0640`，只掛給需要的服務（`compose:35-47`；`compose_policy.py` 的 `SERVICE_SECRETS`）。
  - KEK 只在 bot，webapi 持有就拒絕開機。
  - 每一把秘密都有寫好的換法與驗證步驟（`secret-rotation.md`）。
  - 交易所 key 關閉 withdraw 權限，開機時與每 6 小時重查（`ARCHITECTURE.md` §10c）。
- 共同的重新評估觸發：第一把不屬於 owner 的 Bitfinex key 進系統（開放外部 real-money beta 之前），或需要解密的稽核紀錄。屆時改用 KMS multi-version 包每把 key 的 DEK（ADR-0831 Amendment (2026-10-22)）。

| 編號 | 缺的控制 |
|---|---|
| V11.1.1 | 每把 key 的產生、換檔、驗證、撤銷、持有者與備份 passphrase 的換法都已寫在 `secret-rotation.md`；缺的是定期輪替與 cryptoperiod：輪替由事件觸發（疑似外洩、裝置或人員變動、換主機），不排定期輪替。 |
| V11.2.2 | 可以換 key（產生新 KEK，同時以 `bfx-op rotate-credential` 換一把新的 Bitfinex key，舊列退役：`secret-rotation.md`「Vault KEK」），但演算法與 key 版本不能替換：`core/crypto.py:21` 固定 `_CURRENT_KEY_VERSION = 1`，解密不看 key 版本，也沒有演算法識別；多 KEK 輪替刻意不實作（`modules/credentials/vault.py:34-42`）。 |
| V13.3.1 | 秘密不在原始碼與 image 裡，常駐服務改成秘密檔，但沒有 key vault 或 KMS：KEK 是主機上的檔案；一次性容器（每週報告、研究、operator CLI）的秘密以 env 傳入（`ARCHITECTURE.md` §10c）。 |

### 離機備份的完整性與防竄改的 log 保存

適用：V11.3.3、V16.4.2、V16.4.3。

公開紀錄：`ARCHITECTURE.md` §10g（異地備份列）、§10c（log 列「沒有防竄改的離機保存」）、§10a（主機等同 root）；`offsite-dr.md:182-183`；`docs/adr/2026-09-04-offsite-dr-cloudflare-r2.md:33`。

- 共同理由：
  - pgBackRest 只提供 `aes-256-cbc` 的 repository 加密。
  - R2 的 Bucket Lock 可能讓 pgBackRest 的 `expire` 永久失敗（`docs/adr/2026-09-04-pgbackrest-in-postgres-image-and-staged-isolated-restore.md:22`），所以目前停用。
  - 防竄改的離機保存（R2 加 Bucket Lock）延到放貸恢復時再一起做。
  - 主機上 `ubuntu` 的 sudo 與掛 docker.sock 的監控容器等同 root，屬於上一節的單一 VM 決定。
- 共同的重新評估觸發：放貸恢復時，與 R2 Bucket Lock 一起重評。

| 編號 | 缺的控制 | 補償控制 |
|---|---|---|
| V11.3.3 | 應用層的加密都是 authenticated（vault 用 AES-GCM 且綁 AAD，換憑證用 HPKE）；但異地備份是 `repo1-cipher-type=aes-256-cbc`（`deploy/vm/pgbackrest/pgbackrest.conf:8`），沒有 MAC，加密層偵測不到 R2 上密文被竄改。 | pgBackRest 的 manifest checksum；每月 restore test 在隔離副本上還原並驗證（`operations.md` §8）。 |
| V16.4.2 | bot 與 webapi 的 log 在主機上是 Docker json-file，root 與 `docker` group 能讀寫（`operations.md:245-246`），沒有 append-only 或防竄改機制。 | log 同時送到主機以外（Loki、Sentry、Telegram，見 V16.4.3）；json-file 有大小上限（V16.1.1）。 |
| V16.4.3 | 一般 log 已離開主機：homelab 的 alloy 讀 container log 送 Loki，保留 30 天（`operations.md:246`）；錯誤送 Sentry，告警送 Telegram。但 alloy 與 Loki 的設定與傳輸方式不在本 repo，無法從這裡確認傳輸安全，也無法確認主機被入侵時改不到。 | Grafana 規則對 log 告警（`ARCHITECTURE.md:445`）；Sentry 與 Telegram 是第三方 SaaS，不在主機上。 |

### 營運入口的 TLS 由 Tailscale 管理

適用：V12.1.1、V12.1.2。

公開紀錄：`ARCHITECTURE.md` §10e「TLS（已接受的控制）」。

- 缺的控制：TLS 只由 tailnet 內的 Tailscale Serve 終結（`serve.json`：`"443": {"HTTPS": true}`）。協定版本（V12.1.1）與 cipher suite（V12.1.2）由 Tailscale（Go `crypto/tls`）決定，本 repo 與 VM 都不能設定。關閉 Funnel、入口只剩 tailnet 之後，也還沒有量過實際的協定版本與 cipher。
- 理由：流量在進 Serve 之前已經走 WireGuard，只有 tailnet 內、被 grant 允許的裝置連得到，TLS 是第二層。
- 補償控制：
  - WireGuard 傳輸。
  - tailnet grant 只讓 operator 連 `tag:bfx-ops` 的 `tcp:443`（`fresh-host-setup.md:215-219`）。
  - 憑證由 Tailscale 自動申請與更新。
  - tailscale image 釘 digest（`compose:158,183`）。
- 重新評估觸發：
  - 營運入口不再只在 tailnet 內，例如改用 identity-aware proxy 或公開入口（ADR-1015 Revocation Triggers）。
  - 從 tailnet 裝置量測協定版本與 cipher 之後，依結果改判。

### UI 沒有閒置時限

適用：V7.3.1。

公開紀錄：ADR-1015 Amendment (2026-10-24)（不設 idle 時限的決定與 Rationale）；`ARCHITECTURE.md` §10h。

- 缺的控制：UI 沒有 inactivity timeout。只要 operator 裝置連在 tailnet、node key 未到期，UI 不論閒置多久都可用。
- 理由：Tailscale node key 只有 absolute 到期、沒有 idle 時限；要在 app 層做 idle，得把已移除的 app session 加回來。
- 補償控制：
  - UI 只有讀取與降低曝險的寫入（kill、停用幣別、config draft）。最壞的結果是停止放貸，不是資金移動。
  - 閒置的風險由 operator 裝置的螢幕鎖承擔。
  - UI 的 session 上限是 node key 到期（180 天）。
  - 裝置遺失時先在 admin console 移除它（§10h 的終止步驟）。
- 重新評估觸發：
  - UI 新增會擴大曝險的寫入。
  - 開放第二個使用者。
  - Tailscale 提供 idle 或裝置狀態的控制。
  - IdP 帳號的驗證因子改變。

### 修改驗證相關屬性前的重新驗證

適用：V7.5.1。

公開紀錄：ADR-1015 Amendment (2026-10-15)「check mode 的重新驗證間隔」與 Revocation Triggers。

- 缺的控制：
  - app 沒有 email、電話或 MFA 設定，會影響驗證的屬性是 tailnet policy（grant、SSH 規則，在 Tailscale admin console）與 membership 表。
  - membership 只有 table owner 能寫（`backend/scripts/grant_operator_membership.py:51-55`），owner 憑證在主機上，進主機要經 Tailscale SSH check mode。
  - check mode 是 12 小時的窗口，窗口內不會再驗證，所以不算「修改前完整重新驗證」。
- 理由：tailnet 目前的方案不能把 check mode 設成每次連線都驗證（`checkPeriod: always`），所以採 Tailscale 預設的 12 小時。
- 補償控制：
  - Tailscale SSH 只允許非 root 使用者。
  - CLI 每次呼叫都重新以 `tailscale whois` 取身分，並記到 journald（`operations.md:737-740`）。
  - 修改 tailnet policy 要另外登入 Tailscale admin console。
- 重新評估觸發：tailnet 改用付費方案，或開始保管外部使用者的資金。屆時改為 `checkPeriod: always`。

## V1 Encoding and Sanitization

符合 14、不符合 0、不適用 13、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V1.1.1 | L2 | 符合 | 前端是 static export（`frontend/next.config.ts:4-11`），沒有自行解碼的程式：`rg -n "unquote\|decodeURIComponent\|decodeURI\b" backend/src frontend/src` 無結果。webapi 只靠 Starlette 解碼一次 path／query，再交給 pydantic（帳號 id 是 `UUID`，`modules/api/account_scope.py:48-51`；symbol 是 `Path(pattern=SYMBOL_PATTERN)`，`trading_control.py:230`）。靜態檔由 `StaticFiles` 以 realpath＋commonpath 限定在 export 目錄內（`ingress.py:261`；starlette `staticfiles.py:160-173`）。 |
| V1.1.2 | L2 | 符合 | HTML 由 React 在 build 與 client render 時跳脫，`frontend/src` 沒有 `dangerouslySetInnerHTML`。SQL 走 SQLAlchemy bind。JSON 由 FastAPI response model 序列化（`schemas.py:1-9`）。Telegram 以純文字送出、沒有 `parse_mode`（`modules/observability/alerts.py:243`）。log 在輸出時由 `json.dumps(..., ensure_ascii=True)` 編碼，控制字元一律跳脫（`core/log_format.py:6-8,54`）。 |
| V1.2.1 | L1 | 符合 | HTML 交給 React 跳脫。webapi 動態組出的 header 只有 CSP，插入的只是 sha256 base64 雜湊（`ingress.py:71-95`），其餘安全 header 是常數（`ingress.py:55-62`）。CSV 的 `Content-Disposition` 是公開站 `_headers` 裡的常數（`modules/live_validation/public_site.py:138-149`）。 |
| V1.2.2 | L1 | 符合 | 前端的 URL 以同源 `/api/v1` 為底，query 一律用 `URL.searchParams.set`（`frontend/src/lib/api-client.ts:18,51-59`）；帳號 path 只接受標準 UUID（`api-client.ts:21-33`）；symbol 用 `encodeURIComponent`（`frontend/src/features/dashboard/hooks/use-trading-control.ts:135`），後端再以 pattern 驗證。後端唯一的 redirect 目的地是常數 `HOME`（`ingress.py:50,242-244`）。 |
| V1.2.3 | L1 | 符合 | JSON 全部由 FastAPI response model 序列化（PR #248，`schemas.py:1-9`），沒有手工拼 JS 或 JSON 字串。UI 的 CSP 是 `script-src 'self'` 加 export 內每段 inline script 的 sha256，沒有 `'unsafe-inline'`／`'unsafe-eval'`（`ingress.py:71-95`；`tests/modules/api/test_ingress.py::test_the_static_ui_carries_the_hash_csp_and_the_security_headers`、`::test_the_policy_lists_exactly_the_given_hashes`）。 |
| V1.2.4 | L1 | 符合 | runtime 查詢全部經 SQLAlchemy ORM 或 `text()` 加 bind 參數（例：`core/writer_lock.py:71`）。f-string SQL 只在 alembic migration 與 ops 腳本，插入的是模組常數表名或程式產生的 role 名（例：`backend/alembic/versions/c6d7e8f9a0b1_ledger_seed_schema.py:178`、`deploy/vm/pgbackrest/ledger_digest.py:189-191`、`deploy/vm/pgbackrest/restore_drill.py:611-623`），沒有 request 輸入。 |
| V1.2.5 | L1 | 符合 | 所有 `subprocess` 都用 argv list，沒有 `shell=True`／`os.system`／`os.popen`（`rg -n "shell=True\|os\.system\|os\.popen" backend/src deploy` 無結果）：例 `modules/observability/resource.py:35`、`bfx_op.py:344`、`deploy/vm/ops/bfx_restore_test.py:58`、`deploy/vm/pgbackrest/restore_drill.py:135,157`。`bfx_op.py` 把 operator 的參數當 argv 元素傳給 `compose run`，不經 shell。 |
| V1.2.6 | L2 | 不適用 | 沒有 LDAP：依賴清單（`backend/pyproject.toml`、`frontend/package.json`）與 `backend/src` 都沒有 LDAP（`rg -i ldap` 無結果）。身分是 Serve 轉送的 operator capability（`auth.py:1-9,66-81`）。 |
| V1.2.7 | L2 | 不適用 | 不處理 XML，也沒有 XPath：`rg -i "xml\|lxml\|xpath\|ElementTree\|DOMParser" backend/src frontend/src` 無結果。 |
| V1.2.8 | L2 | 不適用 | 沒有 LaTeX 處理器（依賴與程式碼皆無）。 |
| V1.2.9 | L2 | 不適用 | 沒有用不受信任的輸入組 regex。前端唯一的動態 `RegExp` 由常數 locale 清單組成（`frontend/src/components/layout/sidebar-nav.tsx:17`）；後端的 regex 都是字面常數（例：`ingress.py:63`、`modules/observability/error_tracking.py:41-43`、`modules/simulated_venue/_internal/wire.py:155-162`）。 |
| V1.3.1 | L1 | 不適用 | 沒有 WYSIWYG 或使用者 HTML 輸入；前端依賴沒有 rich-text editor（`frontend/package.json`）。 |
| V1.3.2 | L1 | 符合 | `frontend/src`、`backend/src` 沒有 `eval(`／`new Function`／`exec(`（rg 只命中測試裡的 `RegExp.exec`）。UI 的 CSP 不含 `'unsafe-eval'`（`ingress.py:71-85`）。 |
| V1.3.3 | L2 | 符合 | 首次 bootstrap 與換憑證走同一個 `validate_credential`（可列印 ASCII、無空白、有長度上限；`modules/execution/credential_bootstrap.py`；`tests/modules/execution/test_credential_bootstrap.py::test_a_key_the_rotation_path_would_refuse_is_refused_before_bitfinex`）；換憑證的 label 拒絕控制字元（`tests/modules/execution/test_credential_rotation.py::test_a_label_with_a_control_character_is_refused`）。PR #296。 |
| V1.3.4 | L2 | 不適用 | 沒有使用者上傳或提交 SVG 的功能：API 沒有檔案上傳端點（E1），有 body 的寫入只收 JSON（`ingress.py:153-156`）。 |
| V1.3.5 | L2 | 不適用 | 沒有使用者提供的 Markdown／CSS／XSL／BBCode；前端依賴沒有 markdown renderer（`frontend/package.json`）。 |
| V1.3.6 | L2 | 符合 | 對外請求的目的地都固定：Bitfinex URL 是常數（`external/bitfinex/ws.py:25`、`external/bitfinex/auth_ws.py:368`），Telegram host 是常數、token 先以 pattern 驗證（`modules/observability/alerts.py:237,315`）。`/fe-events` 只轉送到設定的 DSN 所屬 project，目的地只由設定組出、不取自 envelope（`ingress.py:184-198`；`tests/modules/api/test_ingress.py::test_fe_events_forward_to_the_configured_project_only`、`::test_fe_events_refuse_another_dsn`）。 |
| V1.3.7 | L2 | 符合 | 沒有伺服器端 template engine（`rg -i "jinja\|Template\(" backend/src` 無結果）。next-intl 訊息是 `frontend/messages/*` 的靜態檔，在 static export 時 render，使用者資料只當參數值。 |
| V1.3.8 | L2 | 不適用 | 沒有 Java，也沒有 JNDI。 |
| V1.3.9 | L2 | 不適用 | 沒有 memcache 或 Redis：`rg -i "redis\|ioredis\|memcache" backend/pyproject.toml frontend/package.json` 無結果；`backend/src` 只剩 enum 常數與註解（`core/telemetry.py:44`、`modules/api/ratelimit.py:8`）。 |
| V1.3.10 | L2 | 符合 | 沒有把變數當格式字串：`rg "\w\.format\(\|format_map" backend/src` 只命中 `logging.Formatter.format`（`core/log_format.py:66`）。log 用 `%s` 加參數，或以 `extra` 欄位交給 JSON formatter（`core/security_log.py`）。 |
| V1.3.11 | L2 | 不適用 | 系統不寄信：`rg -i "smtp\|nodemailer\|sendmail" backend/src frontend/src deploy` 無結果。 |
| V1.4.1 | L2 | 不適用 | 應用程式碼只有 Python 與 TypeScript（記憶體託管語言），沒有自寫的 C 或 unsafe 程式碼。 |
| V1.4.2 | L2 | 符合 | 金額用 `Decimal`，在鎖內檢查 `is_finite()` 且 `> 0`，並與 budget 上限比較（`modules/ledger/_internal/journal.py:166-169,286`）；venue 規則另有 `validate_amount`（`modules/execution/command_gate.py:261-262`）。API 的範圍值要求有限、`gt=0` 且 min≤max（`schemas.py:151-167`），list 有 `le=_MAX_LIMIT`（100，`modules/api/uncertainties.py:49,213`）。Python int 不會溢位。 |
| V1.4.3 | L2 | 不適用 | 記憶體託管語言，沒有手動配置記憶體或指標。 |
| V1.5.1 | L1 | 不適用 | 不解析 XML（同 V1.2.7）。 |
| V1.5.2 | L2 | 符合 | 不受信任的輸入只走 JSON 加 pydantic，寫入 body 拒絕未知欄位（`trading_control.py:56-57`、`schemas.py:158,178`）。YAML 只用 `yaml.safe_load`（`apps/config.py:228,314`、`modules/backtest/cell_pipeline.py:134`），以及讀本地 config 的 ruamel（`cell_pipeline.py:70`）。`rg "pickle\|marshal\|read_pickle\|allow_pickle\|yaml\.load\(\|shelve" backend/src deploy` 無結果。 |

## V2 Validation and Business Logic

符合 11、不符合 0、不適用 0、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V2.1.1 | L1 | 符合 | webapi 每個輸入的型別、長度、pattern、白名單與 `additionalProperties`，寫在由 pydantic 產生、進版控的 `backend/openapi.json`；CI 重新產生並 `git diff --exit-code`（`.github/workflows/ci.yml:41-53`，PR #248）。前端表單規則註明只是 UX，以型別斷言綁定後端 body（`frontend/src/lib/validations.ts:28-34`）。CLI 憑證輸入的規則寫在 `core/sealed_credential.py:85-97`。 |
| V2.1.2 | L2 | 符合 | `ARCHITECTURE.md` §6 定義組合條件：`OfferEnvelopeGuard` 的 `max_offer_amount`、天期 [min,max]、利率下限、同幣別 open offer 數（`ARCHITECTURE.md:413`）。Range 的 min≤max 寫在 `schemas.py:151-167`，也帶進 `openapi.json`。 |
| V2.1.3 | L2 | 符合 | 上限有寫出：webapi 每個 operator 每分鐘讀 120 次、寫 30 次（`modules/api/ratelimit.py:1-9`；`ARCHITECTURE.md` §10d）；全域送單 token bucket（`operations.md:431-432`，`pre_trade_limits.command_rate`；`ARCHITECTURE.md:431`）；CapitalPolicy 包絡與資金上限（`ARCHITECTURE.md:411-413`）；自動 resume 次數上限（`modules/execution/safety/trading_state.py:21-22`；`operations.md:140`）。 |
| V2.2.1 | L1 | 符合 | `StrategyConfigBody` 與 `Range` 拒絕未知欄位與非有限值，currency 為 `^[A-Za-z0-9]{1,16}$`（`schemas.py:151-185`；`tests/test_config_schemas.py::test_only_the_known_finite_shape_is_accepted`）；前端 Zod 同步（`frontend/src/lib/validations.ts`）。PR #296。 |
| V2.2.2 | L1 | 符合 | 驗證以 webapi 為準：前端 Zod 只是 UX，以 `AssertAssignable` 綁到後端 body 型別（`frontend/src/lib/validations.ts:28-34`）；後端獨立執行同樣的規則（`schemas.py:151-185`；`tests/test_config_schemas.py::test_negative_amount_rejected`、`::test_min_greater_than_max_rejected`、`::test_empty_currency_rejected`；`tests/test_config_router.py::test_put_invalid_returns_422_or_400`）。憑證由 CLI 輸入、在伺服器端驗證（`core/sealed_credential.py:85-97`；PR #218 移除了 web 的憑證表單）。 |
| V2.2.3 | L2 | 符合 | Range 要求 min≤max（`schemas.py:163-167`）。送單時 audit row 的 rate／duration 必須與 decision 相同，否則 `execution_audit_conflict`（`modules/execution/command_gate.py:216-222`）。包絡的利率、天期、金額組合由 `OfferEnvelopeGuard` 檢查（`ARCHITECTURE.md:413`）。 |
| V2.3.1 | L1 | 符合 | 每個私有請求都先驗 operator capability（`auth.py:66-81`），再驗帳戶 membership（`modules/api/account_scope.py:48-51`），沒有可跳過的步驟（`tests/architecture/test_webapi_route_guard.py::test_private_routes_require_the_operator`、`::test_the_walk_catches_an_unguarded_route`）。trading state 的轉移規則在程式與 DB trigger 雙重強制（`modules/execution/safety/trading_state.py:120-145`；`tests/modules/execution/safety/test_trading_state.py::test_illegal_transitions_are_rejected_in_code`、`::test_legal_lifecycle_is_recorded_in_order`）。送單要求 decision row 的 `outcome == "ready"`（`command_gate.py:218-222`）。operator request 只會由 `requested` 轉為 `applied` 或 `rejected`（`tests/integration/test_trading_control.py::test_a_transition_the_rules_refuse_is_a_rejection_not_a_fault`）。 |
| V2.3.2 | L2 | 符合 | 文件寫的上限都有實作：鎖內 budget 檢查（`modules/ledger/_internal/journal.py:163-169`；`tests/integration/test_ledger_command_authorize.py::test_first_pass_budget_refusal_writes_nothing`）；`CommandThrottle`（`modules/execution/safety/pre_trade.py:169`，由 `command_gate.py:180,446` 的 `_admit` 套用）；auto-resume 的最短停機時間與次數（`trading_state.py:54,140-143`）；webapi 的 per-user token bucket（`tests/test_ratelimit.py::test_read_limit_429_with_retry_after`、`::test_write_bucket_independent_of_read`）。kill：`tests/integration/test_kill_switch.py::test_kill_writes_halted_then_cancels_managed_orphan_and_unknown_currencies`、`::test_kill_waits_for_an_in_flight_command_after_writing_halted`、`::test_a_wedged_command_does_not_hold_the_kill_hostage`。 |
| V2.3.3 | L2 | 符合 | intent 的授權、budget 與 insert 在同一個 transaction（`command_gate.py:216-248`、`journal.py:107-135`）。venue 呼叫刻意放在兩個 transaction 之間，沒落地的結果以 PENDING→UNKNOWN 收斂，outcome 寫不進去時 process 直接退出（`command_gate.py:1-6,290-320`）。budget 被拒時什麼都不寫（`test_ledger_command_authorize.py::test_first_pass_budget_refusal_writes_nothing`）；kill 寫不進 HALTED 時不碰 venue（`test_kill_switch.py::test_no_venue_call_when_halted_cannot_be_written`）。`get_session` 出錯就 rollback（`modules/api/deps.py:51-62`）。 |
| V2.3.4 | L2 | 符合 | 可用資金由兩層鎖保護：同 process 內每個帳戶一把 `asyncio.Lock`（`command_gate.py:176-180`），跨 process 用 per-scope `pg_advisory_xact_lock`（`modules/ledger/_internal/clock.py:22-27`、`journal.py:70,119`），budget 在鎖內重讀（`journal.py:144-169`）；同 symbol 的 amount fingerprint 也在鎖內檢查碰撞（`command_gate.py:224-237`）。測試：`tests/integration/test_capital_command_boundary.py::test_independent_command_gates_cannot_spend_same_budget`、`::test_halt_writer_waits_for_authorization_lock`。 |
| V2.4.1 | L2 | 符合 | `/fe-events` 要 operator capability，並有自己的 token bucket（每個 operator 每分鐘 60 則，429＋Retry-After），不佔 API 寫入額度（`ingress.py:53,216-220`；`tests/modules/api/test_ingress.py::test_fe_events_need_the_operator_capability`、`::test_fe_events_have_their_own_bucket`）；私有路由有 per-user bucket（`modules/api/ratelimit.py`）。webapi 沒有不驗證的 API 端點（`tests/architecture/test_webapi_route_guard.py` 的 `PUBLIC_API_PATHS` 是空集合）。PR #295、#281。 |

## V3 Web Frontend Security

符合 17、不符合 0、不適用 2、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V3.2.1 | L1 | 符合 | 非 UI 回應（`/api/*`、`/fe-events`）帶 `Content-Security-Policy: sandbox; default-src 'none'; frame-ancestors 'none'`（`ingress.py:55,121-122`；`tests/modules/api/test_ingress.py::test_api_answers_render_nothing_and_are_never_stored`），另有 `nosniff`。PR #295。 |
| V3.2.2 | L1 | 符合 | `rg 'dangerouslySetInnerHTML\|innerHTML\|insertAdjacentHTML\|document\.write' frontend/src --glob '!**/__tests__/**'` 無結果；畫面全由 React JSX 輸出，字串以文字節點插入。 |
| V3.3.1 | L1 | 符合 | 系統不寫任何 cookie：next-intl `localeCookie: false`（`frontend/src/i18n/routing.ts`；`frontend/src/providers/__tests__/query-provider.test.ts`），語系由 URL 決定；app 沒有 session cookie；webapi 不設 cookie（`rg -i "set_cookie\|set-cookie" backend/src` 只命中 Sentry 清洗清單）。PR #298。 |
| V3.3.2 | L2 | 符合 | 同 V3.3.1：沒有 cookie，所以沒有需要設 `SameSite` 的 cookie；身分來自 Serve 轉送的 tailnet capability（`auth.py:66-81`）。 |
| V3.3.3 | L2 | 符合 | 同 V3.3.1：沒有 cookie，不需要 `__Host-` 前綴。PR #298。 |
| V3.3.4 | L2 | 不適用 | 沒有 session token 或任何不該讓 JS 讀到的 cookie：身分是 Serve 注入的 `Tailscale-App-Capabilities` header（`auth.py:25-27,66-81`；`serve.json` 的 `AcceptAppCaps`），不經 cookie 或 response body 交給瀏覽器。 |
| V3.4.1 | L1 | 符合 | `Strict-Transport-Security: max-age=63072000; includeSubDomains`（`ingress.py:61`）由最外層 ASGI middleware 加在每個回應上，含 `/api/*`、`/fe-events`、static export 與 probe（`ingress.py:102-136`；以 `add_middleware` 掛載：`ingress.py:215`）。PR #237。沒有測試直接斷言 HSTS，同一個 dict 的其他 header 有斷言（`tests/modules/api/test_ingress.py::test_the_static_ui_carries_the_hash_csp_and_the_security_headers`）。 |
| V3.4.2 | L1 | 符合 | 整個 app 不發 `Access-Control-Allow-Origin`：`rg -i 'Access-Control\|CORSMiddleware\|allow_origins' backend/src frontend/src` 無結果；設計上不開 CORS（ADR-1015「營運入口」）。 |
| V3.4.3 | L2 | 符合 | UI 的 CSP 有 `object-src 'none'` 與 `base-uri 'none'`（`ingress.py:71-85`；`tests/modules/api/test_ingress.py::test_the_static_ui_carries_the_hash_csp_and_the_security_headers`）；API 回應是 sandbox CSP。PR #295。 |
| V3.4.4 | L2 | 符合 | `X-Content-Type-Options: nosniff` 加在每個回應上（`ingress.py:58,124-133`），含 JSON、static 檔與 `/fe-events`；公開站的 `_headers` 也帶（`public_site.py:142`）。 |
| V3.4.5 | L2 | 符合 | `Referrer-Policy: strict-origin-when-cross-origin` 加在每個回應上（`ingress.py:59,124-133`）；`connect-src 'self'`（`ingress.py:80`），不向第三方載資源。 |
| V3.4.6 | L2 | 符合 | UI 與 API 回應都帶 `frame-ancestors 'none'`（`ingress.py:55,82`；`tests/modules/api/test_ingress.py::test_api_answers_render_nothing_and_are_never_stored`），另有 `X-Frame-Options: DENY`（`ingress.py:57`）。PR #295。 |
| V3.5.1 | L1 | 符合 | ingress middleware 對 GET/HEAD/OPTIONS 以外的請求要求：`Sec-Fetch-Site`（有送時）為 same-origin 或 none、`Origin` 必須存在且等於 `https://<allowed host>`、`Host` 在 allowlist（`ingress.py:138-157`）；這層在所有 route 之前（`ingress.py:102-120`）。測試：`test_ingress.py::test_a_cross_site_post_is_refused`、`::test_a_bodiless_cross_site_delete_is_refused`。PR #237。 |
| V3.5.2 | L1 | 符合 | 不依賴 preflight：除了 Origin 檢查，有 body 的寫入只收 `application/json`（`ingress.py:153-156`），`text/plain`、form、multipart 回 415；測試 `test_ingress.py::test_a_post_without_json_is_refused`、`::test_a_delete_with_a_non_json_body_is_refused`。 |
| V3.5.3 | L1 | 符合 | webapi 的狀態變更都是 POST/PUT/DELETE：kill、停用幣別（`trading_control.py:214,227`）、config draft（`modules/api/config.py:98,116`）；其餘 route 都是 GET（`rg '@router\.(post\|put\|delete\|patch)' backend/src/bfx_funding_bot/modules/api`）；bot 的 `/admin/halt`、`/admin/dry-evaluate` 是 POST（`router.py:82,111`）；static mount 對 GET/HEAD 以外回 405（starlette `staticfiles.py:119-120`）。 |
| V3.5.4 | L2 | 符合 | 唯一入口是 tailnet 節點的 Serve，`serve.json` 只有一個 handler `/` → `http://127.0.0.1:8000`；webapi 只聽 netns 的 loopback（`compose:109,143`），沒有其他 app 共用這個 hostname；沒有公開入口（`fresh-host-setup.md:209-213`）。 |
| V3.5.5 | L2 | 不適用 | 沒有 postMessage 接收端：`rg 'postMessage\|addEventListener\("message"\|onmessage' frontend/src` 無結果。 |
| V3.7.1 | L2 | 符合 | 前端只用 Next.js（`frontend/package.json:26`）＋React，以 static export 發布（`frontend/next.config.ts:9`）；沒有 Flash、Java applet、ActiveX 之類的外掛技術。 |
| V3.7.2 | L2 | 符合 | 沒有由使用者輸入決定目的地的 redirect：webapi 只有 `/` → 常數 `HOME`（`ingress.py:50,242-244`），static mount 只做補斜線的同站 redirect（starlette `staticfiles.py:140-151`）；前端只有固定站內路徑（`frontend/src/app/[locale]/page.tsx:11`）。`safeCallbackUrl` 沒有呼叫端（`rg safeCallbackUrl frontend/src --glob '!**/__tests__/**'` 只命中定義 `frontend/src/lib/safe-callback-url.ts:4`）。 |

## V4 API and Web Service

符合 5、不符合 0、不適用 5、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V4.1.1 | L1 | 符合 | webapi 的 JSON 回 `application/json`；static export 的 HTML／JS 由 `StaticFiles` 依副檔名給 `text/*`，Starlette 對 `text/*` 自動補 `; charset=utf-8`（starlette `responses.py:77-78`；`ingress.py:261`）。webapi 已不提供下載（PR #281）。 |
| V4.1.2 | L2 | 符合 | 唯一入口是 Serve 的 TCP 443 HTTPS，沒有 80 port handler，也沒有 Funnel（`serve.json:2-6`），所以沒有對 API 做透明的 HTTP→HTTPS redirect。webapi 只綁 `127.0.0.1:8000`，在 netns 內，沒有 published port（`compose:109,143`）。每個回應都帶 HSTS（`ingress.py:61,124-133`）。 |
| V4.1.3 | L2 | 符合 | webapi 用到的中介 header 只有 Serve 設定的 `Tailscale-App-Capabilities` 與 `Tailscale-User-Login`（`auth.py:25-27,66-81`）。Serve 會覆寫 client 自送的 `Tailscale-*` header（ADR-1015 `:123`，實測）。webapi 只聽 netns 內的 127.0.0.1，Serve 是唯一 client（`compose:109,143`）。webapi 不讀 `X-Forwarded-*`／`X-Real-IP`：rg 只命中 Sentry 送出前刪掉這些 header 的清單（`modules/observability/error_tracking.py:45-46`）。 |
| V4.2.1 | L2 | 符合 | 每一層都用標準 parser、沒有放寬設定：webapi 與 bot `/admin` 都跑 uvicorn，`http` 為預設的 `auto`（`compose:109`、`modules/marketfeed/healthz.py:159-170`），裝有 httptools 時就用它（`backend/pyproject.toml:8` `uvicorn[standard]`；`backend/uv.lock:839-840`）。前端是 static export，沒有 Node server（`frontend/next.config.ts:4-11`）。Serve 屬第三方。 |
| V4.3.1 | L2 | 不適用 | 沒有 GraphQL：`rg -i "graphql\|strawberry\|ariadne\|apollo"` 在依賴清單與程式碼無結果，API 只有 REST router（`apps/webapi.py:126-133`）。 |
| V4.3.2 | L2 | 不適用 | 同上，沒有 GraphQL。 |
| V4.4.1 | L1 | 符合 | 系統唯一的 WebSocket 是 bot 連到 Bitfinex 的 client 連線，URL 是 `wss://` 常數（`external/bitfinex/ws.py:25`、`external/bitfinex/auth_ws.py:368`）。`rg 'ws://' backend/src frontend/src` 無結果。 |
| V4.4.2 | L2 | 不適用 | 不提供 WebSocket server 端點：webapi 與 bot `/admin` 都沒有 websocket route（E3），前端是 static export。 |
| V4.4.3 | L2 | 不適用 | 同上，沒有 WebSocket server，也沒有 WS session token。 |
| V4.4.4 | L2 | 不適用 | 同上。 |

## V5 File Handling

符合 3、不符合 0、不適用 6、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V5.1.1 | L2 | 不適用 | 沒有任何上傳功能（E1），本條要求每個 upload feature 都有文件。唯一的下載（funding-rates CSV）是 Cloudflare Pages 上的靜態檔，每週由 `modules/live_validation/public_site.py:111`（`funding_rates_csv`）從 DB 數值產生（PR #278、#281）。`/fe-events` 只是 Sentry 遙測轉送，不存檔（`ingress.py:220-240`）。 |
| V5.2.1 | L1 | 不適用 | 不接受檔案（E1）；JSON request body 不屬檔案處理。`/fe-events` 的 envelope 有 1 MiB 上限、邊讀邊截斷且不落地（`ingress.py:51,201-209,225-229`）。 |
| V5.2.2 | L1 | 不適用 | 不接受檔案（E1）。 |
| V5.2.3 | L2 | 不適用 | 不接受壓縮檔。唯一的解壓是離線 backtest 讀 repo 內自產的 `.jsonl.gz` fixture（`modules/backtest/fixture_io.py:66`，呼叫端 `modules/backtest/cell_pipeline.py:17`），不是外部輸入。 |
| V5.3.1 | L1 | 不適用 | 沒有由不受信輸入上傳或產生、再存進公開目錄的檔案。webapi 只讀 build 時就放進 image 的 static export（`ingress.py:257-261`）；`frontend/public/` 只有 build 時的圖示與 manifest，runtime 不寫入（E4）。公開站的檔案由排程工作從 DB 數值產生（`public_site.py`），不含使用者提供的內容。 |
| V5.3.2 | L1 | 符合 | request-facing 程式（`modules/api`、`modules/admin`）不用使用者輸入組檔案路徑：只有 FastAPI 的 `Path(pattern=SYMBOL_PATTERN)` 參數驗證（`trading_control.py:230`）、`modules/api/deps.py:43` 以 `__file__` 為基準的路徑，以及啟動時讀 export 算 CSP 雜湊（`ingress.py:88-95`）。靜態檔請求由 `StaticFiles` 以 realpath＋commonpath 擋住跳出目錄（starlette `staticfiles.py:160-173`）。SSRF：`/fe-events` 的目的地只由設定的 DSN 組出（`ingress.py:184-198`；`test_ingress.py::test_fe_events_refuse_another_dsn`）。 |
| V5.4.1 | L2 | 符合 | 檔名固定為 `funding-rates-{fUST,fUSD}.csv`（`public_site.py:29-32` 的 `CSV_SYMBOLS`／`SITE_FILES`；上傳端只收這些檔：`deploy/vm/ops/bfx_public_site.py` 的 `collect`）；Pages 的 `_headers` 為每份 CSV 送 `Content-Disposition: attachment; filename="funding-rates-<symbol>.csv"`（`public_site.py:138-149`；`tests/modules/live_validation/test_public_site.py::test_headers_forbid_script_and_send_csvs_as_downloads`）。 |
| V5.4.2 | L2 | 符合 | 送出的檔名只由常數組成：固定前綴加 `CSV_SYMBOLS` 的兩個值（`public_site.py:29-32,145-148`），不含使用者可控的字元，不會破壞 header 結構；檔案清單由 `tests/modules/live_validation/test_public_site.py::test_the_site_is_exactly_the_files_the_uploader_accepts` 鎖住。 |
| V5.4.3 | L2 | 不適用 | 沒有提供來自不受信來源的檔案下載：CSV 由 `funding_rates_csv` 把 DB 的數值逐行格式化產生（`public_site.py:111-117`；`test_public_site.py::test_csv_is_hourly_close_as_apr_percent`），沒有使用者上傳的內容。 |

## V6 Authentication

符合 6、不符合 0、不適用 29、待證 0。V6 章多處引用上面的[驗證路徑](#驗證路徑)。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V6.1.1 | L1 | 不適用 | app 不收任何 credential：沒有密碼、OTP 或登入端點（E5）。credential stuffing 與 brute force 打的是 tailnet 的 IdP（GitHub 帳號），由 IdP 防護；app 的身分只來自 Serve 轉送的 capability（`auth.py:66-81`）。這個委派是 ADR-1015「營運入口」與「移除 app 登入」的決定。break-glass 的 `ubuntu` 密碼只在 OCI Console Connection 使用、要先登入 OCI 帳號（`operations.md:782-783`），是主機憑證，不是 app 的驗證。 |
| V6.1.2 | L2 | 不適用 | app 沒有密碼（E5），沒有可套用禁用詞的地方。 |
| V6.1.3 | L2 | 符合 | 各路徑與強度集中在 `ARCHITECTURE.md` §10h 的表：UI＝已登錄裝置＋capability；CLI＝check mode 每 12 小時回 IdP；`halt` 與 Console＝OCI 帳號密碼＋MFA；GitHub 帳號開啟兩步驟驗證；node key 到期 180 天。公網 sshd 已停用（`operations.md` §12）。PR #301。 |
| V6.2.1 | L1 | 不適用 | app 沒有密碼（E5）；舊的 app 密碼登入已隨 PR #237 移除。 |
| V6.2.2 | L1 | 不適用 | 同 V6.2.1；沒有可改的密碼。 |
| V6.2.3 | L1 | 不適用 | 同 V6.2.1。 |
| V6.2.4 | L1 | 不適用 | 同 V6.2.1。 |
| V6.2.5 | L1 | 不適用 | 同 V6.2.1。 |
| V6.2.6 | L1 | 不適用 | 前端沒有密碼欄位（E5：`type="password"` 無結果）。 |
| V6.2.7 | L1 | 不適用 | 同 V6.2.6。 |
| V6.2.8 | L1 | 不適用 | 同 V6.2.1。 |
| V6.2.9 | L2 | 不適用 | 同 V6.2.1。 |
| V6.2.10 | L2 | 不適用 | 同 V6.2.1。 |
| V6.2.11 | L2 | 不適用 | 同 V6.2.1。 |
| V6.2.12 | L2 | 不適用 | 同 V6.2.1。 |
| V6.3.1 | L1 | 不適用 | app 不收 credential，沒有可被猜的密碼或 OTP（同 V6.1.1）。 |
| V6.3.2 | L1 | 符合 | 沒有預設帳號：app 沒有使用者表。唯一被接受的 principal 是設定的 `BFX_OPERATOR_TAILNET_USER_ID`，沒有預設值、不可空白（`core/settings.py:78,90-91`），缺值時 webapi 拒絕開機（`tests/test_operator_auth.py::test_the_web_api_refuses_to_start_without_each_ingress_value`）。membership 只能由 table owner 從既有成員複製（`backend/scripts/grant_operator_membership.py:51-62`）；migration 不種任何成員（`rg -n -i "insert into (public\.)?exchange_account_memberships" backend/alembic` 無結果）。 |
| V6.3.3 | L2 | 符合 | 兩條日常路徑都要 IdP 的多因子：GitHub 帳號開啟兩步驟驗證，tailnet 的登入只有 GitHub，CLI 另有 check mode 每 12 小時回 IdP（`ARCHITECTURE.md` §10h；ADR-1015 Amendment (2026-10-24)）。PR #301。 |
| V6.3.4 | L2 | 符合 | 沒有未記錄的路徑，各路徑的控制一致。(1) UI：每個 private route 經 `require_operator`（`tests/architecture/test_webapi_route_guard.py::test_private_routes_require_the_operator`、`::test_every_route_is_versioned_api_a_probe_or_the_ingress`），不驗證的只有 probe 與 ingress 自己的 `/`、API fallback（同檔的 `PROBE_PATHS`、`UNAUTHED_INGRESS_PATHS`；`PUBLIC_API_PATHS` 是空集合）。(2) CLI：`bfx-op` 除 `halt` 外都要 tailnet peer 與 whois 身分（`bfx_op.py:182-216`；`tests/scripts/test_vm_operator_wrapper.py::test_every_other_subcommand_still_needs_a_tailnet_peer`、`::test_the_identity_is_whois_never_the_callers`）。(3) 兩條路徑共用同一份 membership 與寫入檢查（`modules/execution/operator_commands.py:155-191`）。高風險操作只走 CLI，DB trigger 擋 webapi（`ARCHITECTURE.md:804-807`；`tests/integration/test_web_api_high_risk_writes.py::test_the_web_api_may_kill_but_not_resume`、`::test_the_web_api_may_disable_but_not_enable`、`::test_the_web_api_cannot_write_credentials_or_adjudications`）。不經 tailnet 的只有 Console 下的 `halt`（只降低曝險）與主機 root，信任邊界已寫明（`bfx_op.py:44-50`、`operations.md:716-718,788`）。 |
| V6.4.1 | L1 | 不適用 | 系統不產生初始密碼或啟用碼：app 沒有建立帳號的流程。operator 的身分由 IdP 建立，membership 由 owner 腳本複製（`backend/scripts/grant_operator_membership.py:1-16`）。 |
| V6.4.2 | L1 | 符合 | 沒有 password hint 或 secret question：`rg -n -i "secret.?question\|password.?hint\|security.?question" frontend/src backend/src` 無結果，app 也沒有任何驗證輸入（E5）。 |
| V6.4.3 | L2 | 不適用 | app 沒有密碼可重設；IdP 帳號的復原屬於 GitHub。失去 tailnet 存取時，主機入口是 OCI Console Connection（`operations.md` §12）；它不重設任何因子，也不讓 `bfx-op` 的高風險子命令可用（`operations.md:788`）。 |
| V6.4.4 | L2 | 不適用 | app 沒有可遺失的 MFA 因子；IdP 因子遺失後的復原在 GitHub。break-glass 不會繞過 IdP 讓高風險操作可用（同 V6.4.3）。 |
| V6.5.1 | L2 | 不適用 | app 沒有 lookup secret、OOB code 或時間型 OTP（E5）；舊的 app MFA 已隨 PR #237 移除。 |
| V6.5.2 | L2 | 不適用 | 同 V6.5.1。 |
| V6.5.3 | L2 | 不適用 | 同 V6.5.1。 |
| V6.5.4 | L2 | 不適用 | 同 V6.5.1。 |
| V6.5.5 | L2 | 不適用 | 同 V6.5.1。 |
| V6.6.1 | L2 | 不適用 | 沒有 PSTN／SMS：`rg -n -i "sms\|phone\|magic.?link\|email.?otp\|sendOTP" frontend/src backend/src/bfx_funding_bot/modules/api backend/src/bfx_funding_bot/core` 只命中無關的 `Permissions-Policy` header（`ingress.py:60`）。 |
| V6.6.2 | L2 | 不適用 | 沒有 OOB 機制（同上）。 |
| V6.6.3 | L2 | 不適用 | 沒有 code-based OOB 機制（同上）。 |
| V6.8.1 | L2 | 不適用 | app 只有一個身分來源，就是 Serve 轉送的 operator capability；使用者不能選 IdP，app 也不拿 IdP 給的識別字串比對身分。app 比對的是 tailnet policy 寫進 capability 的 `user_id`（`auth.py:45-54,72-76`），與 IdP 帳號名無關。誰拿得到 capability 由 grant 的 `src` 決定（`fresh-host-setup.md:215-219`）。 |
| V6.8.2 | L2 | 不適用 | app 不收任何簽章的驗證 assertion（JWT／SAML 皆無，E6）。身分由 Serve 以 header 交給同一個 network namespace 裡只聽 loopback 的 webapi（`compose:109,143`；`compose_policy.py`；`tests/scripts/test_vm_compose_app.py::test_the_ingress_shape_is_pinned`、`::test_serve_config_proxies_only_to_the_local_web_api`）；client 自送的 `Tailscale-*` header 會被 Serve 覆寫（ADR-1015 `:123`）。完整性靠通道，不靠簽章。 |
| V6.8.3 | L2 | 不適用 | 沒有 SAML：`rg -n -i "saml\|sso\b" backend/src frontend/src frontend/package.json backend/pyproject.toml` 無結果。 |
| V6.8.4 | L2 | 符合 | app 收不到也不驗 `acr`／`amr`／`auth_time`，一律假設最低強度（持有裝置＋一個可被釣魚的 IdP 因子），因此 UI 只有讀取與降低曝險的寫入，擴大曝險的操作只走 check mode 的 CLI（ADR-1015 Amendment (2026-10-24)；`ARCHITECTURE.md` §10a「動作的分界」、§10h）。PR #301。 |

## V7 Session Management

符合 11、不符合 2、不適用 5、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V7.1.1 | L2 | 符合 | `ARCHITECTURE.md` §10h：UI 的 absolute 上限＝node key 到期 180 天，不設 idle（見 V7.3.1）；CLI 是 check mode 12 小時；偏離 NIST SP 800-63B AAL2 的理由與代價在 ADR-1015 Amendment (2026-10-24) 的 Rationale。PR #301。 |
| V7.1.2 | L2 | 符合 | `ARCHITECTURE.md` §10h「允許的 operator 裝置」：一台 operator 的筆電；新增前先加列，admin console 出現不在清單上的裝置時照終止步驟移除，並換 GitHub 的密碼與兩步驟驗證。PR #301。 |
| V7.1.3 | L2 | 符合 | `ARCHITECTURE.md` §10h「各段時效的關係」（GitHub web session → node key → check mode 12 小時）與「終止 operator 的存取」三步（移除裝置；帳號疑似被盜時換 GitHub 憑證並登出其他 session；撤銷整個人時拿掉 grant、SSH 規則與 membership）。PR #301。 |
| V7.2.1 | L1 | 符合 | 驗證都在 server 端：WireGuard 對端由 tailscaled 驗證；Serve 依 tailnet policy 轉送 capability（`serve.json`）；webapi 每個 request 都以 `require_operator` 檢查（`auth.py:66-81`；`tests/test_operator_auth.py::test_require_operator_refuses_anything_but_the_operator_capability`）；CLI 由 `bfx-op` 在主機上以 `tailscale whois` 取身分（`bfx_op.py:202-216`）。瀏覽器不持有、不驗證任何 token（E5）。 |
| V7.2.2 | L1 | 符合 | 請求的身分來自各裝置的 WireGuard node key（Tailscale 產生，到期時更換），不是 client 出示的靜態秘密。app 不接受 client 出示的任何 token，client 自送的 `Tailscale-*` header 會被 Serve 覆寫（`auth.py:3-8`）。bot `/admin` 的兩把靜態 token 是服務之間的機器憑證，不代表使用者 session（`router.py:1-12,34-46`）。 |
| V7.2.3 | L1 | 不適用 | app 不發 reference token：沒有 cookie、session 表，也不產生 token（E5）。 |
| V7.2.4 | L1 | 不適用 | app 沒有 session token 可輪替或終止（E5）；每個 request 都從 Serve 的 capability 重新取得身分（`auth.py:66-81`）。裝置重新登入 tailnet 時換 node key，屬於 Tailscale。 |
| V7.3.1 | L2 | 不符合 | 已接受：UI 沒有閒置時限，見[UI 沒有閒置時限](#ui-沒有閒置時限)。 |
| V7.3.2 | L2 | 符合 | Tailscale 強制 node key 到期；tailnet 設定是 Tailscale 預設的 180 天，依風險決定不縮短：UI 只有降低曝險的寫入，高風險操作每 12 小時回 IdP（ADR-1015 Amendment (2026-10-24)；`ARCHITECTURE.md` §10h）。重新評估的觸發寫在同一 Amendment。PR #301。 |
| V7.4.1 | L1 | 符合 | app 端沒有任何能比 tailnet session 活得久的狀態：沒有 cookie、cookie cache 或 session 儲存（E5）。每個 request 都由 `require_operator` 依當下 Serve 轉送的 capability 判斷（`auth.py:66-81`）；CLI 每次呼叫都重新 whois（`bfx_op.py:202-216`）。裝置登出、node key 到期或被移除後，下一個 request 就沒有身分。 |
| V7.4.2 | L1 | 符合 | 停用 operator 的兩個位置都在下一個 request 生效。tailnet 端移除使用者、裝置或 grant 後，Serve 不再轉送 capability（`auth.py:68-76` 回 401）。app 端刪除 membership 後，webapi 與 CLI 每次都重查（`modules/api/account_scope.py:48-68` → `modules/execution/operator_commands.py:155-169`；`apps/operator_cli.py:277-284`；`tests/integration/test_operator_cli.py::test_refusals_come_before_the_outbox`、`tests/modules/api/test_account_scope.py::test_nonmember_account_path_is_404`）。bot 套用前再以 `operator_authorized` 重驗（`ARCHITECTURE.md:800`）。 |
| V7.4.3 | L2 | 不適用 | app 沒有驗證因子，也沒有更換因子的流程。因子在 IdP 端變更；裝置 session 在 Tailscale admin console 管理。 |
| V7.4.4 | L2 | 不適用 | app 沒有登入，也就沒有可登出的 app session；UI 的存取綁在裝置的 tailnet 連線上，中斷或登出 Tailscale 即失效（見 V7.4.1）。前端沒有 logout（E5）。 |
| V7.4.5 | L2 | 符合 | 管理者（operator 本人，`tag:bfx-ops` 的 tagOwner 是 `autogroup:admin`：`fresh-host-setup.md:215`）可以在 Tailscale admin console 移除單一裝置或使用者，node key 立即失效；步驟寫在 `ARCHITECTURE.md` §10h「終止 operator 的存取」。app 端沒有 session 要另外清（V7.4.1），刪 membership 立即生效（V7.4.2）。 |
| V7.5.1 | L2 | 不符合 | 已接受：check mode 是 12 小時窗口，見[修改驗證相關屬性前的重新驗證](#修改驗證相關屬性前的重新驗證)。 |
| V7.5.2 | L2 | 不適用 | app 不維護 session，沒有 app session 可列出或終止；裝置 session 的列表與移除在 Tailscale admin console（見 V7.4.5）。 |
| V7.6.1 | L2 | 符合 | app 是 Tailscale 身分的 relying party，但不保留 session：每個 request 依當下的 capability 判斷（`auth.py:66-81`），所以 app 端的存取不會比 tailnet 端的 session 長。CLI 的時效就是 Tailscale SSH check mode 的 12 小時（ADR-1015 Amendment (2026-10-15)；`operations.md:708-710`）；各段時效的關係見 `ARCHITECTURE.md` §10h。 |
| V7.6.2 | L2 | 符合 | app 不建立 session。能使用的前提都需要使用者明確動作：裝置加入 tailnet 要在 IdP 登入並核准；Tailscale SSH check 要在瀏覽器完成驗證（`operations.md:708-710`）。 |

## V8 Authorization

符合 6、不符合 0、不適用 1、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V8.1.1 | L1 | 符合 | `ARCHITECTURE.md` §10a 授權矩陣：每個主體（tailnet UI 的 operator、`sudo bfx-op`、bot、webapi、研究、模擬、`bfx-deploy`、DR、觀測）的身分來源、DB role、bot `/admin` token、venue 寫入與主機權限，每一格附出處。function-level（UI 只有讀取與降低風險的寫入，高風險只走 CLI）與 data-level（membership、非成員一律 404、viewer 只讀：`modules/execution/operator_commands.py:129-131,188-191`、`modules/api/account_scope.py:48-68`）都寫明；§1「Web API identity」寫入口與 capability。 |
| V8.1.2 | L2 | 符合 | `ARCHITECTURE.md` §10a「帳戶內的角色與欄位」：憑證回應的 secret 固定 `****`（`schemas.py:99-108,120-127`）；config draft 只收 `StrategyConfigBody` 宣告的欄位；`bfx_webapi`／`bfx_operator` 對 outbox 只有 request 欄位的欄位級 INSERT，結果欄位只有 bot 能寫（`fresh-host-setup.md:25,80`）；欄位級 grant 由 `test_webapi_privilege_allowlist.py`、`test_operator_privilege_allowlist.py` 鎖住。 |
| V8.2.1 | L1 | 符合 | function-level：webapi 每個 private route 經 `require_operator`（`auth.py:66-81`），由 `tests/architecture/test_webapi_route_guard.py::test_private_routes_require_the_operator`、`::test_every_route_is_versioned_api_a_probe_or_the_ingress` 鎖住；寫入另要 owner／operator，viewer 只讀（`operator_commands.py:188-191`）。webapi 沒有高風險操作的 route，DB trigger 也擋（`tests/integration/test_web_api_high_risk_writes.py::test_the_web_api_may_kill_but_not_resume`、`::test_the_web_api_may_disable_but_not_enable`）。CLI 除 `halt` 外要 tailnet 身分與 membership（`bfx_op.py:182-216`、`apps/operator_cli.py:277-284`）。bot `/admin` 每個 route 都要 token、常數時間比對，只讀 token 不能 halt（`router.py:34-46`；`tests/architecture/test_admin_route_guard.py::test_admin_route_refuses_without_the_token`、`tests/modules/admin/test_router_trading_status.py::test_the_read_token_cannot_halt`）。誰拿得到 capability 由 tailnet grant 決定（`fresh-host-setup.md:215-219`）。 |
| V8.2.2 | L1 | 符合 | data-level：以 account path 的 UUID＋membership 授權，失敗一律 404（`modules/api/account_scope.py:48-68` → `modules/execution/operator_commands.py:155-173`）。子資源查詢綁 `exchange_account_id`（`modules/accounts/exchange_accounts.py:197-201`）；帳戶列表以 membership join（`modules/api/routers.py:45-51`）。測試：`tests/modules/api/test_account_scope.py::test_nonmember_account_path_is_404`、`::test_unknown_and_retired_accounts_are_non_enumerating_404`。 |
| V8.2.3 | L2 | 符合 | 寫入只接受宣告過的欄位：kill／disable 的 `ControlBody` 設 `extra="forbid"`（`trading_control.py:56-57`），config draft 用 `StrategyConfigBody`（`schemas.py:170-185`）。回應是白名單 response model，secret 一律回 `****`（`schemas.py:99-108`；`tests/modules/api/test_response_contract.py`，PR #248）。viewer 不能寫（`test_account_scope.py::test_viewer_can_read_but_cannot_mutate`）。webapi 對請求表只有欄位級 INSERT grant（`ARCHITECTURE.md:797`），憑證表與裁決表的寫入權已收回（`test_web_api_high_risk_writes.py::test_the_web_api_cannot_write_credentials_or_adjudications`）。 |
| V8.3.1 | L1 | 符合 | 授權全在 server 端：webapi 的 `require_operator`／`require_account_member`／`require_account_write`（`auth.py:66-81`、`modules/api/account_scope.py:48-76`）；CLI 用同一份 `operator_commands`（`ARCHITECTURE.md:808`）；bot 套用前以 SQL `public.operator_authorized` 重驗（`ARCHITECTURE.md:800`）。前端是 static export，沒有 server 端程式（`frontend/next.config.ts:4-12`），只影響顯示。 |
| V8.4.1 | L2 | 不適用 | 目前不是 multi-tenant：webapi 只接受一個設定的 operator（`core/settings.py:78`、`auth.py:72-76`），沒有註冊或建立帳戶的流程；本條確屬多租戶行為。帳戶層的 membership 隔離已經存在（V8.2.2）；開放外部帳戶時依 ADR-0831 的目標架構（RLS／scoped roles）重評。 |

## V9 Self-contained Tokens

符合 0、不符合 0、不適用 7、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V9.1.1 | L1 | 不適用 | 系統不收也不簽發 self-contained token：舊的 JWT 與 JWKS 驗證已移除（E6；PR #237、#239）。身分改由 Serve header 傳遞，評估在 V6.8.2。 |
| V9.1.2 | L1 | 不適用 | 同 V9.1.1。 |
| V9.1.3 | L1 | 不適用 | 同 V9.1.1；沒有 JWKS URL 或金鑰來源設定（E6）。 |
| V9.2.1 | L1 | 不適用 | 同 V9.1.1。 |
| V9.2.2 | L2 | 不適用 | 同 V9.1.1。 |
| V9.2.3 | L2 | 不適用 | 同 V9.1.1。 |
| V9.2.4 | L2 | 不適用 | 同 V9.1.1。 |

## V10 OAuth and OIDC

符合 0、不符合 0、不適用 29、待證 0。tailnet 的 IdP 登入由 Tailscale 完成，app 不參與 flow、不接觸任何 token（E2）。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V10.1.1 | L2 | 不適用 | 本系統沒有 OAuth／OIDC（E2），也沒有其他 bearer token（E6）。 |
| V10.1.2 | L2 | 不適用 | 沒有 authorization flow，所以沒有 code、state、nonce 或 PKCE（E2）。 |
| V10.2.1 | L2 | 不適用 | 系統不是 OAuth client（E2）。 |
| V10.2.2 | L2 | 不適用 | 沒有任何 authorization server 要互動（E2）。 |
| V10.3.1 | L2 | 不適用 | 系統不收 access token（E2、E6）。 |
| V10.3.2 | L2 | 不適用 | 同 V10.3.1。授權看 Serve 轉送的 capability 與 membership，見 V8.2.1、V8.2.2。 |
| V10.3.3 | L2 | 不適用 | 同 V10.3.1。使用者以 tailnet user ID 識別，見 V6.8.1。 |
| V10.3.4 | L2 | 不適用 | 同 V10.3.1。app 不驗 IdP 的驗證強度，改採最低強度假設，見 V6.8.4。 |
| V10.4.1 | L1 | 不適用 | 系統不是 authorization server，也沒有 redirect URI（E2）。 |
| V10.4.2 | L1 | 不適用 | 沒有 authorization code（同上）。 |
| V10.4.3 | L1 | 不適用 | 沒有 authorization code（同上）。 |
| V10.4.4 | L1 | 不適用 | 沒有 OAuth client 或 grant（同上）。 |
| V10.4.5 | L1 | 不適用 | 沒有 refresh token（E2 的 `refresh_token` 搜尋無結果）。 |
| V10.4.6 | L2 | 不適用 | 沒有 code grant（同上）。 |
| V10.4.7 | L2 | 不適用 | 沒有 dynamic client registration（同上）。 |
| V10.4.8 | L2 | 不適用 | 沒有 refresh token（同 V10.4.5）。session 的時限不屬本章，見 V7.3。 |
| V10.4.9 | L2 | 不適用 | 沒有 refresh token 或 reference access token（E2、E6）。 |
| V10.4.10 | L2 | 不適用 | 沒有 confidential client 的 backchannel（token、PAR、revocation endpoint）。 |
| V10.4.11 | L2 | 不適用 | 沒有 OAuth client 或 scope。 |
| V10.5.1 | L2 | 不適用 | 系統不是 OIDC relying party，沒有 ID Token（E2）。 |
| V10.5.2 | L2 | 不適用 | 同上。 |
| V10.5.3 | L2 | 不適用 | 同上；不讀 discovery 或 metadata。 |
| V10.5.4 | L2 | 不適用 | 同上。 |
| V10.5.5 | L2 | 不適用 | 沒有 OIDC back-channel logout。 |
| V10.6.1 | L2 | 不適用 | 系統不是 OpenID Provider（E2）。 |
| V10.6.2 | L2 | 不適用 | 同上；沒有 RP-initiated logout。 |
| V10.7.1 | L2 | 不適用 | 沒有 authorization server，也沒有第三方 client，所以沒有授權同意流程（E2）。 |
| V10.7.2 | L2 | 不適用 | 同上。 |
| V10.7.3 | L2 | 不適用 | 同上。 |

## V11 Cryptography

符合 10、不符合 3、不適用 1、待證 0。加密盤點見 `ARCHITECTURE.md` §10g。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V11.1.1 | L2 | 不符合 | 已接受：輪替由事件觸發、沒有定期輪替，見[金鑰保管與輪替](#金鑰保管與輪替)。 |
| V11.1.2 | L2 | 符合 | 加密盤點表：每一種加密或簽章的演算法、key 位置、保護與保護不了的範圍（`ARCHITECTURE.md` §10g）。PR #300。 |
| V11.2.1 | L2 | 符合 | 後端只用 pyca `cryptography`：AESGCM（`core/crypto.py:15,65-67,82-83`）、HPKE suite（`core/sealed_credential.py:22-25,35`；`tests/core/test_sealed_credential.py::test_the_locked_cryptography_has_hpke`）；HMAC 用 stdlib `hmac`（`external/bitfinex/auth_ws.py:336-361`）。前端沒有任何加密依賴（`frontend/package.json`）。沒有自製加密原語。 |
| V11.2.2 | L2 | 不符合 | 已接受：演算法與 key 版本不可替換，見[金鑰保管與輪替](#金鑰保管與輪替)。 |
| V11.2.3 | L2 | 符合 | AES-256（`core/crypto.py:48` 強制 32-byte KEK）、X25519 加 ChaCha20-Poly1305 256-bit key（`core/sealed_credential.py:35`）、HMAC-SHA384（Bitfinex）；KEK 由 `openssl rand -base64 32` 產生（`fresh-host-setup.md:290`），備份 passphrase 由 `openssl rand -base64 48` 產生（`secret-rotation.md:217-218`）。全部 ≥128-bit security。 |
| V11.3.1 | L1 | 符合 | `rg -n -w -i "md5\|sha1\|ECB\|PKCS1v15\|RS256\|rc4\|3des\|blowfish" backend/src deploy frontend/src frontend/instrumentation-client.ts` 無結果。 |
| V11.3.2 | L1 | 符合 | vault 用 AES-GCM（`core/crypto.py:65-67`），換憑證用 HPKE 的 ChaCha20-Poly1305（`core/sealed_credential.py:35`；`test_sealed_credential.py::test_a_tampered_byte_does_not_open`）。pgBackRest 的 aes-256-cbc（`deploy/vm/pgbackrest/pgbackrest.conf:8`）是 approved mode，它沒有 MAC 的部分列在 V11.3.3。 |
| V11.3.3 | L2 | 不符合 | 已接受：異地備份的加密沒有 MAC，見[離機備份的完整性與防竄改的 log 保存](#離機備份的完整性與防竄改的-log-保存)。應用層：vault 的 wrap 與 secret 都綁 AAD（`core/crypto.py:63-67,80-83`；`tests/test_crypto.py::test_tampered_ciphertext_fails`、`::test_wrong_user_aad_fails`），sealed credential 的 `info` 綁帳戶、環境、recipient 與 request（`core/sealed_credential.py:55-62`；`test_sealed_credential.py::test_any_other_context_does_not_open`）。 |
| V11.4.1 | L1 | 符合 | 用到的雜湊：sha256（`modules/ledger/_internal/journal.py:343`、`deploy/vm/pgbackrest/ledger_digest.py:445`、CSP hash `ingress.py:94`）、HMAC-SHA384（`external/bitfinex/auth_ws.py:336-361`）、HKDF-SHA256（HPKE）。md5／sha1 無命中（見 V11.3.1 的指令）。 |
| V11.4.2 | L2 | 不適用 | 應用不儲存任何密碼：app 登入與 `auth` schema 已移除（PR #242；migration `backend/alembic/versions/f836fdee9bf3_retire_auth_schema.py`；`tests/integration/test_auth_schema_retirement.py::test_upgrade_drops_auth_and_the_old_memberships`）。UI 身分由 Serve 轉送 capability（`auth.py:66-81`）。DB role 密碼由 Postgres 自己雜湊，不是應用程式碼。 |
| V11.4.3 | L2 | 符合 | 完整性用的雜湊都是 SHA-256 或 SHA-384：ledger payload digest（`journal.py:343`、`modules/ledger/_internal/resolver.py:220`）、備份 ledger digest（`ledger_digest.py:445`）。輸出長度都 ≥256 bit。 |
| V11.4.4 | L2 | 符合 | pgBackRest 的 passphrase 是導出 key 的來源：設定精靈拒絕短於 32 字的新值並提示以 `openssl rand -base64 48` 產生（`scripts/setup-pgbackrest-r2.sh:305-308`；`tests/scripts/test_offsite_dr_bootstrap.py::test_wizard_refuses_a_short_new_cipher_passphrase`）。PR #300。 |
| V11.5.1 | L2 | 符合 | DEK 與 nonce 用 `os.urandom`（`core/crypto.py:63-66`）；HPKE 的 key 用 `X25519PrivateKey.generate()`（`core/sealed_credential.py:80`）；`secrets.token_hex`（`apps/venue.py:190`、`deploy/vm/pgbackrest/restore_drill.py:186`）；秘密由 `openssl rand` 產生（`fresh-host-setup.md:190`）。CSP 用 inline script 的 hash，不用 nonce（`ingress.py:71-95`）。`uuid4` 只當 ID；`random` 只在離線 backtest（`modules/backtest/distortion.py:18`、`modules/backtest/oos_profitability.py:11`），不當秘密。 |
| V11.6.1 | L2 | 符合 | 應用層唯一的非對稱加密是換憑證用的 HPKE base mode（RFC 9180）`DHKEM(X25519, HKDF-SHA256)`（`core/sealed_credential.py:1-10,35`）：每次 bot 開機以 `X25519PrivateKey.generate()` 產生 key pair，private key 只在記憶體（`:69,80`，`repr=False`）。測試：`test_sealed_credential.py::test_another_recipient_does_not_open`、`::test_a_recipient_key_of_the_wrong_size_is_refused`。image 的 build provenance 由 GitHub attestation（Sigstore）簽章，部署前以 `gh attestation verify` 驗證（`.github/workflows/release.yml:140-141`；`deploy.md` 的部署步驟；PR #251），使用的是標準工具，不是自製簽章。 |

## V12 Secure Communication

符合 3、不符合 5、不適用 1、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V12.1.1 | L1 | 不符合 | 已接受：TLS 協定版本由 Tailscale 管理，見[營運入口的 TLS 由 Tailscale 管理](#營運入口的-tls-由-tailscale-管理)。 |
| V12.1.2 | L2 | 不符合 | 已接受：cipher suite 由 Tailscale 管理，見[營運入口的 TLS 由 Tailscale 管理](#營運入口的-tls-由-tailscale-管理)。 |
| V12.1.3 | L2 | 不適用 | 沒有 mTLS：`rg -i 'mtls\|client.?cert' backend/src frontend/src` 只有無關的註解命中；身分只來自 Serve 注入的 tailnet capability（`auth.py:66-81`）。 |
| V12.2.1 | L1 | 符合 | 唯一的 client 入口是 Serve 的 HTTPS 443（`serve.json:2-6`，沒有 HTTP handler），沒有公開入口（`fresh-host-setup.md:209-213`）；webapi 只聽 netns 內的 `127.0.0.1:8000`、沒有 published port（`compose:109,143`）；每個回應都帶 HSTS（`ingress.py:61,124-133`）；寫入的 Origin 檢查只接受 `https://`（`ingress.py:150-152`；`test_ingress.py::test_a_cross_site_post_is_refused`）。 |
| V12.2.2 | L1 | 符合 | Serve 用 tailnet 的 HTTPS certificates（Tailscale 為 `*.ts.net` 名稱向公開 CA 申請並自動更新）；runbook 要求開啟 MagicDNS 與 HTTPS certificates（`fresh-host-setup.md:213`）；`serve.json` 以 `${TS_CERT_DOMAIN}` 綁定節點的憑證網域；repo 內沒有自簽憑證（`ARCHITECTURE.md` §10e）。 |
| V12.3.1 | L2 | 不符合 | 已接受：VM 內部連線是明文，見[單一 VM 的扁平網路與「主機等同 root」](#單一-vm-的扁平網路與主機等同-root)。對外連線都是 TLS：Bitfinex（`external/bitfinex/auth_rest.py:440`、`auth_ws.py:368`、`ws.py:25`、`live_executor.py:65`）、Telegram（`modules/observability/alerts.py:237`）、`/fe-events` 轉 Sentry 只用設定的 DSN 組 `https://` 目標（`ingress.py:184-198`）。 |
| V12.3.2 | L2 | 符合 | httpx 預設驗證憑證（webapi→Sentry `ingress.py:235`、Bitfinex client），websockets 對 `wss://` 預設驗證；Postgres 只接受 `disable`／`verify-ca`／`verify-full`，拒絕不驗證的 `require`／`prefer`／`allow` 與 asyncpg 的 `ssl=`（`core/settings.py:38-56`；`core/db.py:81-87`；`tests/core/test_settings.py::test_a_mode_that_does_not_verify_is_refused`、`::test_asyncpgs_own_ssl_parameter_is_refused`）；`rg 'verify=False\|CERT_NONE\|NODE_TLS_REJECT_UNAUTHORIZED\|check_hostname *= *False' backend/src frontend/src deploy` 無結果。 |
| V12.3.3 | L2 | 不符合 | 已接受：webapi→bot 是明文 HTTP，見[單一 VM 的扁平網路與「主機等同 root」](#單一-vm-的扁平網路與主機等同-root)。 |
| V12.3.4 | L2 | 不符合 | 已接受：內部沒有 TLS，也就沒有內部 CA，見[單一 VM 的扁平網路與「主機等同 root」](#單一-vm-的扁平網路與主機等同-root)。 |

## V13 Configuration

符合 8、不符合 5、不適用 0、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V13.1.1 | L2 | 符合 | `ARCHITECTURE.md` §10b 通訊矩陣：每條來源→目的的網路、port／協定、認證與是否加密，含 Bitfinex REST／WS 主機、Telegram、Sentry（含 `/fe-events`）、R2、GHCR、Docker Hub、GitHub 與 GitHub attestation（Sigstore）、Tailscale；並寫明沒有使用者可指定的對外位址。 |
| V13.2.1 | L2 | 不符合 | 已接受：服務間的憑證是長期憑證，見[單一 VM 的扁平網路與「主機等同 root」](#單一-vm-的扁平網路與主機等同-root)。有做的部分：Postgres 每個服務有自己的 role（`bfx_bot`、`bfx_webapi`、`bfx_operator`、`bfx_research`、`bfx_sim`；PR #244）；webapi→bot 用 webapi 專屬、只能讀的 `BFX_ADMIN_READ_TOKEN`（PR #228；以秘密檔讀入：`modules/api/funding_status.py:120`），常數時間比對（`router.py:42-43`）。 |
| V13.2.2 | L2 | 不符合 | 已接受：網路沒有分段，見[單一 VM 的扁平網路與「主機等同 root」](#單一-vm-的扁平網路與主機等同-root)。最小權限有做的部分：webapi 的只讀 token 不能 halt（`router.py:125`；`tests/modules/admin/test_router_trading_status.py::test_the_read_token_cannot_halt`；`tests/modules/api/test_funding_status_token.py::test_the_halt_token_alone_is_not_enough`）；webapi 帶 halt token 或 KEK 就拒絕開機（`apps/webapi.py:46-56`；`tests/test_health.py::test_startup_refuses_the_daemon_halt_token`、`::test_startup_refuses_the_vault_kek`）；DB role 依服務分開。 |
| V13.2.3 | L2 | 符合 | 沒有預設憑證：沒有 Redis（`docker-compose.bot.yml:7`）；Postgres role 密碼與 admin／read token 都當場隨機產生（`fresh-host-setup.md:190`；`secret-rotation.md:57,99`）；tailscale 節點第一次登入由管理者核准，換主機時用 tagged、單次使用的 auth key（`fresh-host-setup.md:220,296`；`compose:196` `TS_AUTH_ONCE`）。 |
| V13.2.4 | L2 | 不符合 | 已接受：沒有網路層的 egress allowlist，見[單一 VM 的扁平網路與「主機等同 root」](#單一-vm-的扁平網路與主機等同-root)。應用層的目的地都寫死：Bitfinex、Telegram、Sentry tunnel 的目標只從設定組出，envelope 決定不了目的地（`ingress.py:184-198`；`test_ingress.py::test_fe_events_refuse_another_dsn`）。 |
| V13.2.5 | L2 | 不符合 | 已接受：同 V13.2.4，見[單一 VM 的扁平網路與「主機等同 root」](#單一-vm-的扁平網路與主機等同-root)。 |
| V13.3.1 | L2 | 不符合 | 已接受：沒有 key vault 或 KMS，見[金鑰保管與輪替](#金鑰保管與輪替)。有做的部分：bot 與 webapi 的常駐秘密是 Compose file secrets，`/opt/bfx/runtime/secrets/<service>/<KEY>`，`root:4600 0640`，掛在 `/run/secrets`（`compose:35-47,79-84,124-126,228`；PR #252、#253），由 `core/secret_files.py` 讀取、不看 env（`core/secret_files.py:1-17`）；秘密不在原始碼（gitleaks 掃全歷史：`.github/workflows/security.yml`），也不進 image（`backend/.dockerignore:22-23`）。 |
| V13.3.2 | L2 | 符合 | 每個服務只掛自己的秘密：bot 有 `DATABASE_URL`、`BFX_ADMIN_TOKEN`、`BFX_ADMIN_READ_TOKEN`、`BFX_VAULT_KEK`、`TELEGRAM_BOT_TOKEN`；webapi 只有 `DATABASE_URL`、`BFX_ADMIN_READ_TOKEN`（`compose:79-84,124-126`）；這個分配由 `compose_policy.py` 的 `SERVICE_SECRETS` 在 CI 檢查。webapi 帶 halt token 或 KEK（env 或秘密檔）就拒絕開機（`apps/webapi.py:46-56`；`tests/test_health.py::test_startup_refuses_a_mounted_halt_token`、`::test_startup_refuses_the_vault_kek`）；admin token 依權限拆分，只讀 token 不能 halt（`router.py:77,97,125`），比對是常數時間（`router.py:42-43`）。KEK 只在 bot（PR #247）。 |
| V13.4.1 | L1 | 符合 | image 只從 `backend` context build，frontend 是 named context（`.github/workflows/release.yml:117-119`）；`backend/.dockerignore:28` 排除 `.git/`，`frontend/.dockerignore:4` 排除 `.git`；最終 image 從 UI stage 只複製 `out/`（`backend/Dockerfile:59`）；VM 不 build，只以 digest 部署（`compose:14-19,59-60`）。 |
| V13.4.2 | L2 | 符合 | webapi 沒開 debug，也沒有 docs（`apps/webapi.py:123`）；uvicorn 沒有 `--reload`（`compose:109`）；`log_level` 預設 INFO（`core/settings.py:117`）；前端以 `next build` 產生 production export（`backend/Dockerfile:17`；`frontend/package.json:8`），`next.config.ts` 沒有開 source map。 |
| V13.4.3 | L2 | 符合 | static export 由 `StaticFiles(html=True)` 提供（`ingress.py:261`）；starlette 遇到目錄只送 `index.html` 或 `404.html`，不產生目錄列表（`staticfiles.py:140-158`）；測試 `test_ingress.py::test_an_unknown_page_is_the_export_404`。 |
| V13.4.4 | L2 | 符合 | TRACE 不在 `SAFE_METHODS`，被當成寫入檢查，沒有同源 Origin 就回 403（`ingress.py:48,145-152`）；`/api/*` 已知路徑用別的方法回 405（`ingress.py:247-255`；`test_ingress.py::test_a_known_api_path_with_another_method_answers_405`）；static mount 對 GET/HEAD 以外回 405（starlette `staticfiles.py:119-120`）；沒有任何地方回顯請求。 |
| V13.4.5 | L2 | 符合 | 文件端點已關閉：webapi `docs_url=None, redoc_url=None, openapi_url=None`（`apps/webapi.py:123`；`tests/architecture/test_webapi_route_guard.py` 的 `DOCS_PATHS`），bot 的 healthz app 也沒有 docs（`modules/marketfeed/healthz.py:70`）。bot 的 `/metrics`、`/healthz`、`/readyz` 不需認證、綁 `0.0.0.0:8080`（`apps/bot.py:1003-1004`），是刻意給觀測與部署健康檢查用的，寫在 `ARCHITECTURE.md` §10a／§10b；它們不經 Serve，tailnet 與 VM 外連不到，同網路容器可達的部分屬已接受的扁平網路。 |

## V14 Data Protection

符合 9、不符合 0、不適用 0、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V14.1.1 | L2 | 符合 | `ARCHITECTURE.md` §10c 資料分級：常駐服務的秘密、一次性工作的秘密、備份秘密、交易所憑證、帳務、市場與研究資料、個人資料、log，各列出存放位置與出處。 |
| V14.1.2 | L2 | 符合 | §10c 為每個等級寫了加密、存取、log 規則與 Sentry 遮蔽；§10f 補上保存表（Postgres、R2 4 份 full＋6 份 diff、Loki 30 天、Sentry）、刪除、外洩通報與法規對照（PR #274）。 |
| V14.2.1 | L1 | 符合 | URL 與 query 不帶秘密：UI 只送一般 query（`frontend/src/lib/api-client.ts:51-58`），身分由 Serve 轉送的 header 帶（`auth.py:66-81`），瀏覽器不持有任何 token。webapi 沒有寫入憑證的 route（`modules/api/api_keys.py:67` 只有 GET；`tests/test_api_keys_router.py::test_credential_writes_are_not_routes`）。換憑證走 CLI，key 與 secret 只從 stdin 讀、不上 argv（`apps/operator_cli.py:20,101-104`；`operations.md:751-755`）。 |
| V14.2.2 | L2 | 符合 | 沒有 CDN、快取 proxy 或 Redis：Serve 直接轉送到 `http://127.0.0.1:8000`（`serve.json:10-14`），`rg -i redis deploy` 無結果。webapi 沒有回應快取，唯一的 `lru_cache` 存 alembic head（`modules/api/deps.py:30-31`）。 |
| V14.2.3 | L2 | 符合 | CSP `connect-src 'self'`（`ingress.py:80`），沒有第三方 tracker；瀏覽器 Sentry 經同源 tunnel `/fe-events`，只轉送到設定的 DSN（`ingress.py:184-198,220-240`）。前端 `sendDefaultPii: false`、`beforeSend: scrubEvent`（`frontend/src/lib/sentry.ts:73-76`），後端 `send_default_pii=False` 並依值遮蔽（`modules/observability/error_tracking.py:52-106`）。測試：`frontend/src/lib/__tests__/sentry.test.ts`、`tests/modules/observability/test_error_tracking.py::test_events_on_the_wire_carry_no_secret_personal_data_or_locals`、`::test_secret_file_values_are_scrubbed_on_the_wire`；`NEXT_TELEMETRY_DISABLED=1`（`backend/Dockerfile:16`）。 |
| V14.2.4 | L2 | 符合 | §10c 列的控制都有實作出處（`core/crypto.py:53-85`、`core/secret_files.py:62`、`modules/observability/error_tracking.py:106`、`core/log_format.py:54`）；保存的實作：`deploy/vm/pgbackrest/pgbackrest.conf:9-10`；Loki 保留 30 天（`docs/adr/2026-09-23-venue-refusal-kept-before-classified.md:66`）。 |
| V14.3.1 | L1 | 符合 | 任何 query 或 mutation 收到 401 時清掉 React Query cache（`frontend/src/providers/query-provider.tsx:25-31`；`frontend/src/providers/__tests__/query-provider.test.ts`）。裝置被移出 tailnet 時瀏覽器收不到 401，所以關掉分頁列在終止步驟裡（`ARCHITECTURE.md` §10h）。PR #298。 |
| V14.3.2 | L2 | 符合 | `/api/*` 回應一律 `Cache-Control: no-store`，handler 自己的值會被取代（`ingress.py:127-129`；`tests/modules/api/test_ingress.py::test_an_api_handlers_own_cache_header_is_replaced`）。PR #295。 |
| V14.3.3 | L2 | 符合 | 瀏覽器端不存任何資料：沒有 Web Storage 或 IndexedDB（`rg -i "localStorage\|sessionStorage\|indexedDB\|document\.cookie" frontend/src` 無結果），也沒有 cookie（V3.3.1）；前端只在 Sentry 清洗時提到 cookie（`frontend/src/lib/sentry.ts:14-15,50`）。 |

## V15 Secure Coding and Architecture

符合 13、不符合 0、不適用 0、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V15.1.1 | L1 | 符合 | `AGENTS.md:76` 依 CVSS 定修補期限（critical 7／high 30／medium 90／low 180 天）；`AGENTS.md:74` 與 `.github/dependabot.yml` 規範一般更新（7 天冷卻）；`osv-scanner.toml` 與 `image-os-updates.toml` 每個條目的 `ignoreUntil` 等於期限，過期 CI 失敗。 |
| V15.1.2 | L2 | 符合 | 語言套件有 lockfile 並由 osv-scanner 掃描；image 另附 BuildKit SBOM attestation（SPDX，`docker/buildkit-syft-scanner` 釘 digest：`.github/workflows/release.yml:123-124`），盤點 OS 與 Python 套件，可用 `docker buildx imagetools inspect <ref> --format '{{json .SBOM}}'` 讀取。postgres image（pgBackRest 由原始碼編譯）不在這份 SBOM 內，由 `deploy/vm/postgres/Dockerfile` 的釘版與 checksum 盤點。PR #302。 |
| V15.1.3 | L2 | 符合 | `ARCHITECTURE.md` §10d 高成本工作：restore test、備份、每週報告、研究回補、整合測試、Bitfinex 已驗證端點、公開 CSV 與私有 API，各列出在哪裡跑與上限（unit 的 `TimeoutStartSec`、腳本的 request 間隔與 429 冷卻、observation request 上限、送單 token bucket、per-user 429）。 |
| V15.2.1 | L1 | 符合 | 語言套件：osv-scanner 掃 lockfile，依 CVSS 期限（`osv-scanner.toml`）。image OS 層：`.github/workflows/security.yml` 的 `image_os_updates` job（main push、每週排程、手動）在最新 release 裡跑 `apt list --upgradable`，`scripts/image_os_updates.py` 對 Debian 已發布、image 未裝的安全更新失敗，暫時裝不了的列在 `image-os-updates.toml` 並有到期日；osv-scanner 對 image 的完整報告寫進 job summary、不擋（實測大部分條目 Debian 尚未出修補，擋下會無法處理，所以只擋已有修補的）。`tests/scripts/test_image_os_updates.py`。PR #303。 |
| V15.2.2 | L2 | 符合 | 依據文件是 `ARCHITECTURE.md` §10d，各項控制都已實作：私有 API 的 per-user token bucket，429 有記錄（`modules/api/ratelimit.py:1-9,102`）；`/fe-events` 1 MiB 上限並邊讀邊截斷（`ingress.py:51,201-209`），轉送 Sentry 的 timeout 10 秒（`ingress.py:235`）；前端 fetch 30 秒 timeout（`frontend/src/lib/api-client.ts:68-76`）；bot 狀態查詢 timeout 15 秒（`modules/api/funding_status.py:123`）；每週報告的步驟各有 `timeout`（`deploy/vm/ops/docker-compose.weekly-report.yml:76-89`）。公開 CSV 是 Cloudflare Pages 的靜態檔，流量不到 VM（PR #281）。 |
| V15.2.3 | L2 | 符合 | `backend/Dockerfile` 拆成 `build`（編譯工具、uv、`uv sync`）與 `runtime`（只 COPY venv 與程式碼；`backend/Dockerfile:45-59`），`tests/scripts/test_backend_image.py::test_the_runtime_stage_is_last_and_installs_nothing` 鎖住 runtime 不安裝任何東西。文件端點關閉（`apps/webapi.py:123`），webapi 沒有公開端點（PR #281）。PR #302。 |
| V15.3.1 | L1 | 符合 | 回應走明確的 pydantic DTO（PR #248：response model 是唯一來源）：憑證回應的 `api_secret` 固定是 `****`（`schemas.py:120-127`，`modules/api/api_keys.py:42-53` 逐欄對映）；公開站只輸出百分比，不輸出金額（`public_site.py:10`）。測試：`tests/test_api_keys_router.py::test_list_masks_secret`；`tests/modules/live_validation/test_public_site.py::test_no_file_carries_a_dollar_figure`。 |
| V15.3.2 | L2 | 符合 | 後端對外呼叫不跟隨 redirect：httpx 預設 `follow_redirects=False`（httpx 0.28.1 `_client.py:197,654`）；明確設定的有 `modules/api/funding_status.py:123-124`、`external/bitfinex/funding_rules.py:119`；`/fe-events` 的 client 用預設值（`ingress.py:235`）。前端只有同源 fetch，不帶任何憑證（`frontend/src/lib/api-client.ts:61-76`）。 |
| V15.3.3 | L2 | 符合 | 每個寫入端點都有固定欄位的 pydantic body：`StrategyConfigBody`（`schemas.py:170-185`；`modules/api/config.py:98-114` 只存 `model_dump`）、`ControlBody` 設 `extra="forbid"`（`trading_control.py:56-57`）。uncertainty 的寫入只走 CLI，webapi 只有 GET（`modules/api/uncertainties.py:210-241`）。帳戶與使用者等身分欄位一律取自 auth context，不取自 body（`modules/api/config.py:107-110`、`operator_commands.py:214,239`）。 |
| V15.3.4 | L2 | 符合 | webapi 不依 IP 做任何決策，也不讀 `X-Forwarded-For`（`rg -n -i "x-forwarded\|x-real-ip\|request\.client" backend/src` 只命中 bot admin 拒絕時記 peer 位址，`router.py:52`，以及 Sentry 刪除這些 header，`error_tracking.py:45-46`）；限流以 user_id 為 key（`modules/api/ratelimit.py:1-4`）。身分 header 只信 Serve：webapi 只聽 netns 內的 127.0.0.1（`compose:109,143`），Serve 會覆寫 client 自送的 `Tailscale-*` header（ADR-1015 `:123`；`auth.py:3-8`）。 |
| V15.3.5 | L2 | 符合 | TypeScript 開 `strict: true`（`frontend/tsconfig.json:7`）、Biome recommended（`frontend/biome.json:27`）；mypy `strict = true`（`backend/pyproject.toml:86-87`）；輸入以 pydantic 或 zod 驗證型別。 |
| V15.3.6 | L2 | 符合 | `rg -n "Object\.assign\|__proto__\|lodash\|\[key\]\s*=" frontend/src --glob '!**/__tests__/**'` 無結果；唯一的動態物件組裝是 Sentry 清洗的 `Object.fromEntries`（`frontend/src/lib/sentry.ts:33,54`），輸入來自 SDK event，不是使用者控制的 key path。 |
| V15.3.7 | L2 | 符合 | 前端以 `searchParams.set` 組 query，重複參數收斂成單一值（`frontend/src/lib/api-client.ts:53-58`）；FastAPI 依宣告的來源（Query、Path、Body、Header）分別取值；有 body 的寫入必須是 JSON（`ingress.py:153-156`），並受 pydantic schema 約束。 |

## V16 Security Logging and Error Handling

符合 14、不符合 2、不適用 0、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V16.1.1 | L2 | 符合 | log 格式與安全事件表在 `operations.md:239-267`；保存與讀取者：bot／webapi 的 json-file 每個最多 5×50 MB（`compose:72-76,118-122`，`compose_policy.py` 的 `capped_log` 檢查），Loki 30 天，主機上 root 與 `docker` group 可讀（`operations.md:245-246`）。PR #299。 |
| V16.2.1 | L2 | 符合 | 每行 JSON 帶 `ts`（UTC）、`level`、`service`、`logger`、`msg` 與事件欄位（`core/log_format.py:37-54`）；bot（`apps/bot.py:1107`）與 webapi（`apps/webapi.py:75,118`；uvicorn access log 也導進來，`log_format.py:90-93`）都用它。安全事件帶 who：Serve 轉送的 tailnet login、node user ID（`core/security_log.py:1-8,32-38`）；operator 的寫入請求在 outbox 記 `requested_by`（`modules/execution/operator_commands.py:214,239`）。測試：`tests/core/test_log_format.py::test_each_process_writes_json_lines_to_stderr`。 |
| V16.2.2 | L2 | 符合 | 時間格式一律 UTC（`core/log_format.py`）；主機時鐘：`bfx-backup-check` 在 `NTPSynchronized` 不是 `yes` 時告警（`deploy/vm/ops/bfx_backup_check.py:172` `clock_problems`；`tests/scripts/test_vm_ops_alerts.py::test_an_unsynchronized_clock_warns_once_confirmed`）。PR #299。 |
| V16.2.3 | L2 | 符合 | log 的去向都列在 `ARCHITECTURE.md` §10c 的 log 列：bot、webapi 的 JSON 寫 stderr 進 docker log，由 homelab 的 alloy 送 Loki（`operations.md:241,246`）；`bfx-op` 寫 journald（`bfx_op.py:165-167`）；Sentry（`modules/observability/error_tracking.py:106`）與 Telegram 告警；pgBackRest 的 log 已關閉（`deploy/vm/pgbackrest/pgbackrest.conf:14-17`）。程式碼沒有其他 log sink（`ARCHITECTURE.md:445`「Telemetry 與 diagnostics 分流」）。 |
| V16.2.4 | L2 | 符合 | bot 與 webapi 輸出同一種 JSON 格式（`core/log_format.py:32-54`），uvicorn 的 logger 交給 root、清掉它自己的 handler（`log_format.py:75-94`）；operational telemetry 的事件 JSON 放在 `msg` 裡，查法見 `operations.md:248-252`。測試：`test_log_format.py::test_configure_logging_owns_a_bare_root_once`、`::test_the_grafana_rule_substrings_still_match_the_raw_line`。 |
| V16.2.5 | L2 | 符合 | 憑證欄位 `repr=False`（`external/bitfinex/credentials.py:10-11`、`core/sealed_credential.py:69,75-76`；`tests/test_secret_reprs.py::test_daemon_admin_token_hidden`）；Telegram 告警依值遮蔽，含秘密檔的值（`modules/observability/alerts.py:195-198,433-445`）；httpx log 裡的 Telegram token 會遮蔽（`alerts.py:214-224`；`tests/modules/observability/test_alerts.py::test_httpx_request_log_never_carries_the_bot_token`）；安全事件從不記 token、帶 token 的 header 或 body（`core/security_log.py:5-7`）；Sentry 清洗見 V14.2.3。 |
| V16.3.1 | L2 | 符合 | 失敗記 `access_denied`（`auth.py:57-63`）；成功的每個 operator 請求記 `access_granted`（INFO，method／path／login；`auth.py:79-80`；`tests/test_operator_auth.py::test_the_operator_is_logged_as_let_through_with_the_login`）。PR #297。 |
| V16.3.2 | L2 | 符合 | 授權失敗都記 `access_denied`：webapi 的 401（`auth.py:57-63`）、非成員或 viewer 寫入（`modules/api/account_scope.py:42,66`；`tests/modules/api/test_account_scope.py::test_a_nonmember_and_a_viewer_write_are_logged_as_access_denied`）、bot `/admin` 的 401／403（`router.py:49-53`；`tests/modules/admin/test_router_trading_status.py::test_a_refused_admin_call_is_logged_without_the_credential`）。事件表見 `operations.md:255-265`。 |
| V16.3.3 | L2 | 符合 | 422 記 `input_rejected`（失敗的欄位路徑與 login，不含值），回應仍是 FastAPI 的 422（`schemas.py:63` `install_request_validation_log`；`tests/modules/api/test_request_validation_log.py`）。ingress 拒絕記 `request_rejected`（`ingress.py:160-166`）。PR #297。 |
| V16.3.4 | L2 | 符合 | 未處理的例外送 Sentry：bot（`apps/bot.py:1109`）、webapi 搭配 Starlette／FastAPI integration（`apps/webapi.py:120-121`）、瀏覽器（`frontend/instrumentation-client.ts:13` 經 tunnel、`frontend/src/app/global-error.tsx:14-16`）；venue 的網路錯誤與 5xx 分成 transient，4xx 與 auth 分成 fatal（`external/bitfinex/retry.py:11-12,40-48`）；拒絕開機記 CRITICAL `boot_refused`（`apps/webapi.py` 的 `lifespan`）。測試：`test_error_tracking.py::test_webapi_starts_error_tracking_with_the_fastapi_integration`、`::test_bot_main_starts_error_tracking_before_the_daemon`。 |
| V16.4.1 | L2 | 符合 | 全域 JSON formatter 用 `json.dumps(ensure_ascii=True)`，CR、LF、控制字元與 Unicode 換行字元都被跳脫（`core/log_format.py:6-8,54`）；request 來的字串有長度上限（`core/security_log.py:20-32`）；`/admin/halt` 的 `actor`、`reason` 是欄位，不進訊息（`router.py:138`）。測試：`test_log_format.py::test_control_characters_in_any_value_cannot_start_a_line`、`test_router_trading_status.py::test_halt_reason_and_actor_cannot_forge_a_log_line`。 |
| V16.4.2 | L2 | 不符合 | 已接受：主機上的 log 沒有防竄改機制，見[離機備份的完整性與防竄改的 log 保存](#離機備份的完整性與防竄改的-log-保存)。 |
| V16.4.3 | L2 | 不符合 | 已接受：離機 log 的傳輸與保存設定不在本 repo、無法確認不可竄改，見[離機備份的完整性與防竄改的 log 保存](#離機備份的完整性與防竄改的-log-保存)。 |
| V16.5.1 | L2 | 符合 | webapi 沒開 debug（`apps/webapi.py:123`），未處理例外由 Starlette 回 500 純文字；HTTPException 只回 reason code（例：`auth.py:63`、`modules/api/api_keys.py:57-58`）；response 驗證失敗回通用的 `response_shape_mismatch`，log 不帶值（`schemas.py:43-57`）；前端 error boundary 在 production 顯示通用訊息（`frontend/src/app/global-error.tsx:22-24`、`frontend/src/app/[locale]/(dashboard)/error.tsx:18-20`）。 |
| V16.5.2 | L2 | 符合 | venue 錯誤分成 transient 與 fatal，只重試 transient（`external/bitfinex/retry.py:11-12`）；Bitfinex verify 遇到 transient 錯誤不降級（`tests/test_vault_service.py::test_verify_500_is_transient_does_not_demote`）；loop watchdog 與依賴就緒檢查（`docs/adr/2026-10-07-loop-watchdog-and-dependency-readiness-replace-autoheal.md`）；webapi 在 DB 不可用時 `/ready` 回 503（`apps/webapi.py:141-148`）；Sentry 連不到時 `/fe-events` 回 502（`ingress.py:234-239`）。 |
| V16.5.3 | L2 | 符合 | 授權設定缺失時一律拒絕：沒有 IngressSettings 時回 503（`auth.py:37-42`、`ingress.py:139-141`），沒有 capability 或對不上回 401（`auth.py:69-76`）；webapi 帶 halt token 或 KEK 就拒絕開機（`apps/webapi.py:46-56`；`tests/test_health.py::test_startup_refuses_the_vault_kek`）；KEK 不符回 503（`test_vault_service.py::test_verify_wrong_kek_raises_key_mismatch`）；具 withdraw 權限的 key 被拒（`::test_verify_withdraw_enabled_fails_closed`）；執行中 key 被拒或不合規時 exit 78（PR #247；`tests/apps/test_bot_loop_watchdog.py::test_a_key_refused_while_running_exits_78_as_a_fatal_error`）。 |

## V17 WebRTC

符合 0、不符合 0、不適用 7、待證 0。

| 編號 | 級別 | 判定 | 依據 |
|---|---|---|---|
| V17.1.1 | L2 | 不適用 | 沒有 WebRTC 或 TURN（E3）；部署的服務只有 postgres（`docker-compose.bot.yml`）與 bot、webapi、netns、tailscale（`compose`）。 |
| V17.2.1 | L2 | 不適用 | 沒有 DTLS 或 media server（E3）。 |
| V17.2.2 | L2 | 不適用 | 同上。 |
| V17.2.3 | L2 | 不適用 | 同上，沒有 SRTP。 |
| V17.2.4 | L2 | 不適用 | 同上。 |
| V17.3.1 | L2 | 不適用 | 沒有 signaling server（E3）。WebSocket 只有 bot 對 Bitfinex 的 outbound client，不是本系統提供的 server。 |
| V17.3.2 | L2 | 不適用 | 同上。 |

## 不存在性證據

V5、V6、V7、V9、V10、V17 章的「不適用」多半是「系統沒有這個功能」。以下指令在 commit `9ddd941f` 的 repo root 重跑過；結果為空，或只有已逐一排除的無關命中。

- **E1 上傳**：
  - `rg -n -i "type=\"file\"|FileReader|new FormData|formData\(\)|\.arrayBuffer\(\)" frontend/src` → 無。
  - `rg -n -i "UploadFile|Form\(|File\(|request\.form" backend/src` → 只有無關命中（`Path(...).is_file()`、`UserProfile`、`ensure_user_profile`、`get_profile`、`period_profile`、`gzip.GzipFile`），沒有上傳端點。
  - `rg -n -i multipart backend/pyproject.toml backend/uv.lock` → 無（FastAPI 沒有 `python-multipart` 就無法收 Form 或 File）。
  - `rg -n -i "upload|dropzone|multer|formidable|busboy" frontend/package.json` → 無。
- **E2 OAuth／OIDC**：`rg -n -i "oauth|oidc|openid|socialProviders|genericOAuth|code_verifier|pkce|authorization_code|refresh_token" frontend/src backend/src deploy frontend/package.json backend/pyproject.toml` → 無。tailnet 的 IdP 登入由 Tailscale 完成，app 不參與 flow、不接觸任何 token。
- **E3 WebRTC 與 WebSocket server**：
  - `rg -n -i "webrtc|RTCPeerConnection|turn:|stun:|coturn|srtp|dtls|getUserMedia|signaling" frontend/src backend/src deploy docs frontend/package.json backend/pyproject.toml` → 只有型別註記 `NoReturn:` 含的 `turn:`。
  - `rg -n -i "websockets\.serve|\.websocket\(|WebSocketRoute|websocket_route" backend/src` → 無：`websocket` 只出現在 bot 對 Bitfinex 的 outbound client 及其使用端。
- **E4 檔案寫入與靜態目錄**：
  - `rg -n "write_text|write_bytes|open\([^)]*['\"]w|mkstemp|NamedTemporaryFile|StaticFiles|FileResponse" backend/src deploy` → ops 腳本（`deploy/vm/ops/*.py`、`deploy/vm/pgbackrest/*.py`）與離線 backtest（`modules/backtest/fixture_io.py:54`、`modules/backtest/cell_pipeline.py:114`）寫 VM 本機路徑，路徑都不是 request 來源；`ingress.py:261` 以 `StaticFiles` 唯讀提供 image 內的 static export。
  - `frontend/public/` 只有 `icon-192.png`、`icon-512.png`、`manifest.json`。
  - `rg -n "writeFile|createWriteStream" frontend/src` → 無。
- **E5 app 層驗證與 session**：
  - `rg -n -i "password|totp|otp|passkey|webauthn|backup.?code" frontend/src backend/src/bfx_funding_bot/modules/api backend/src/bfx_funding_bot/core/auth.py backend/src/bfx_funding_bot/apps/webapi.py` → 只有 `LocaleRootPage` 的子字串。
  - `rg -n 'type="password"' frontend/src` → 無。
  - `rg -n -i "set_cookie|cookie|session_token|SessionMiddleware|logout" backend/src/bfx_funding_bot/modules/api backend/src/bfx_funding_bot/apps/webapi.py backend/src/bfx_funding_bot/core` → 無。
  - `rg -n -i "localStorage|sessionStorage|document\.cookie|logout|sign.?out|indexedDB" frontend/src` → 無。
  - `frontend/src/lib/auth.ts`、`frontend/middleware.ts`、`frontend/src/app/api/` 不存在；前端是 static export（`frontend/next.config.ts:4-12`）。
- **E6 self-contained token**：`rg -n -i "jwt|jwks|jose|pyjwt" backend/src backend/pyproject.toml frontend/src frontend/package.json` → 只剩兩行過時的 docstring（`trading_control.py:5`、`modules/api/account_scope.py:57`），沒有任何程式碼或依賴。JWT 與 JWKS 已由 PR #239 移除，app 登入已由 PR #237 移除。
