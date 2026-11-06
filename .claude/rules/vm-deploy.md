---
description: VM deploy tooling (bfx-deploy) and one-shot ops jobs
paths:
  - "deploy/vm/ops/**"
---

# VM deploy

- 新版由上一版的 `bfx-deploy` 部署：退役它依賴的變數或欄位要分兩個 release，第二步與觸發條件寫進 Pending。
- 跨部署才觸發的 systemd-run job 不可指向 `releases/<rev>`（`bfx-deploy` 只留 current 與 previous），改指向 state dir 或 `current`。
