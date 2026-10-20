# Release 0 operator-only containment runbook（已失效）

這份 runbook 描述 Better Auth 時期的 operator bootstrap（TOTP enrollment 後指派唯一 `admin`）與
non-operator containment（ban 其他帳號、撤銷 session 與 MFA marker）。9d-5 起營運 UI 與 webapi 只經
Tailscale Serve 進入，Better Auth、登入頁、TOTP 與這兩個 one-shot 工具都已刪除，本文件不再適用。

現在誰是 operator：

- tailnet policy 對 `tag:bfx-ops` 的 grant 只給 operator，並帶 capability
  `github.com/Will413028/bfx-funding-bot/cap/operator`，其 `user_id` 是 operator 的 tailnet user ID；
  webapi 只接受這個值等於 `BFX_OPERATOR_TAILNET_USER_ID` 的請求（`backend/ARCHITECTURE.md`「Web API identity」）。
- 帳戶權限仍是 `exchange_account_memberships`，鍵就是這個 tailnet user ID（W3 寫入）。
- 建置步驟：[fresh host setup §2](fresh-host-setup.md#2-tailnet-入口營運-ui)；高風險操作只走 CLI
  （[operations §10](operations.md#10-operator-clibfx-op)）。

Better Auth 的 `auth` schema、帳號與 `bfx_webauth` role 在 9d-7 的 migration 刪除；在那之前它們只留作回滾到
9d-5 之前版本時使用。
