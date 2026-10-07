---
title: 以 seed 直接切換 capital authority（不做零差異證明、豁免模擬 soak），並在切換後立即退場 legacy authority
date: 2026-10-06
status: active
supersedes: "[2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 的 D7'''（切換程序）與 D6（既有歷史的封存方式）"
tags: [bfx-funding-bot, decision, ledger, migration, postgresql, disaster-recovery]
---

# 以 seed 直接切換，切換後立即退場 legacy authority

## Context

[2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) 的 D7''' 規定：在單次 halt 內 seed、以 C（capital comparison）證明新舊讀者零差異、彩排過整個 halt 後才切換；2026-10-03 的 Amendment 再加上模擬 venue 上的 soak 作入場條件。切換工具（seed `--switch`：seed＋closure＋epoch 同一 transaction）與 24 h soak 門檻已在 2026-10-05 上線。

**約束**：

- `external` 只有一位使用者、無外部資金：零差異證明與彩排保護的對象只剩自己，計畫性 halt 隨時可接受（Will 2026-10-05、10-06 確認）。
- `external` 真錢放貸中，venue 上有真實的 resting offer 與 credit：切換不得撤單，切換後仍要能歸屬它們。
- `external` prod 已套用的 migration 不能改寫；epoch 表只能 append。
- `inherited` bfx-deploy 在 migration 期間只停 bot、webapi 繼續跑上一版 image，migration 後不 downgrade（[2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)）：仍成立，是部署工具的現行行為，所以任何收權限或刪欄都要比停寫晚一個 release。
- `inherited` restore test 是含 migration 部署的閘門，用 target release 的 drill 搭已安裝的 wrapper 與部署中的 image（[2026-10-06-ledger-restore-verification-replaces-prefix-test](2026-10-06-ledger-restore-verification-replaces-prefix-test.md)）：仍成立。

## Options Considered

**切換程序**

- **基準：並行比對後再切**（parallel run／[GitHub Scientist](https://github.com/github/scientist)、[Fowler ParallelChange](https://martinfowler.com/bliki/ParallelChange.html)）：新舊讀者對同一輸入比對到零差異才切。即 D7''' 的 C＋runner＋halt 彩排。
- **A. seed 後直接切換**：halt 內以 closure 驗證 seed 涵蓋所有 live 歸屬依賴（不符即 rollback），seed 與 epoch 同 transaction 寫入；不做 C、不跑 runner、不彩排。
- **B. 撤掉所有掛單後以空帳開始**：不需 seed，但違反「不撤單、保留歸屬」。

**入場條件（24 h 模擬 soak）**

- **基準：canary／soak 通過才切**（[Google SRE Workbook, Canarying Releases](https://sre.google/workbook/canarying-releases/)）。
- **C. 以工具旗標豁免並把理由寫進 evidence**（`--waive-soak <理由>`，與 `--soak-result` 互斥）。
- **D. 手寫一份 PASS 結果檔，或繞過工具手動切換**。

**切換後的 legacy 退場（S1-8）**

- **E1 收縮時機**：立即刪 legacy runtime（R2 回滾在第一筆 runtime 觀測寫入時已失效）／等 24 h watch 結束／更晚。
- **E2 全新資料庫的起點**：migration 在無 legacy 歷史時 append `ledger` epoch（genesis）／新增 genesis observation 類型／prod 直接拿掉 seed 開機檢查／保留 seed 程式當 genesis。
- **E3 凍結的 12 張 legacy 表**：搬進 `legacy_archive` schema、撤銷寫入、只授回仍需的讀取／原地留在 public 靠 trigger／dump 到 offsite 後 drop。

## Decision

- **D1 切換程序選 A**（Will 2026-10-05）。halt 只做：停寫入者 → 備份 → seed＋closure＋epoch（同一 transaction）→ 以新 digest recreate → 等第一個 ledger basis。resting offer 保留；24 h 後的 watch 只報告不切回。
- **D2 入場條件選 C**（Will 2026-10-06）：soak 已開跑一代並完成一次部署換代後，Will 決定不等 24 h。豁免只略過 soak 檢查，其他 P-check（digest、schema head、無 runtime 外來 session、seed `--check`）照舊。
- **D3 立即收縮（E1 第一案）**：R2 已關，legacy runtime 與 support set 直接改成只支援 ledger；部署可在 watch 前進行（watch 用自帶的腳本副本）。
- **D4 genesis migration（E2 第一案）**：最新 epoch 為 legacy 且 `event_log` 全空 → append `ledger`；有 legacy 歷史 → migration 失敗。開機只檢查最新 epoch 的寫入者（switch 或 genesis），不逐 scope 查 seed、不讀凍結表。
- **D5 `legacy_archive`（E3 第一案）**：先把 weekly attribution 需要的 legacy offer→cell 解好並物化成它自己的表（衝突比照 journal 側歸為 unattributed），再把 12 張表搬進 `legacy_archive`、撤銷所有非 owner 權限、只授 webapi `event_log` 的 8 欄；寫入一律由無條件 trigger 拒絕（含 owner）。
- **D6 收權限與刪欄跨 release**：停寫、撤權＋ORM 不再 map、DROP 各隔一個 release；依據是上一版 image 實際送出的 SQL。

## Rationale

- **D1 選 A 而非基準**：零差異證明保護的是「切換後歸屬算錯而無法察覺」；但 closure 已在 seed transaction 內證明 seed 涵蓋所有 live 歸屬依賴，切換後每個 reconcile 週期仍以 venue 觀測做逐筆對帳，算錯會以 `unexplained_lending` 立即 HALT。代價：放棄切換前的獨立第二意見與彩排，失效只能在 prod 上 forward-fix。不選 B：撤單會讓真實 credit 的歸屬與放貸收益中斷。
- **D2 選 C 而非 D**：豁免是使用者的權限，但要留下可稽核的紀錄；手寫 PASS 檔等於偽造證據，繞過工具則丟掉已 review 過的 P-check 與 R1／R2／R3 恢復分支。代價：模擬 soak 原本要涵蓋的 kill／恢復與活動下限沒有在切換前驗證，改由切換後的 watch 與每輪對帳承擔。
- **D3**：expand／contract 的 contract 時點由「新路徑寫出第一筆不可逆資料」決定，與經過多久無關（[Fowler ParallelChange](https://martinfowler.com/bliki/ParallelChange.html)）；保留雙 authority 只增加分支與測試成本。
- **D4 而非 genesis observation**：問題只在 epoch，migration 一處就涵蓋 CI、測試、新主機；不選「拿掉 seed 檢查」是因為已切換的資料庫仍需要「epoch 由已知寫入者寫入」這條不變式。開機不讀 `event_log`，D5 的 REVOKE 才不會讓 bot 開不起來。
- **D5 而非原地或 drop**：History 頁仍要顯示切換前資料（產品需求），所以不能 drop；原地保留會讓 legacy ORM 與權限語意無法收乾淨。先物化 attribution，是因為 weekly 每週全量重算、legacy 連結是永久輸入，而它原本依賴可清除的 `diagnostics`。
- **D6**：webapi 在 migration 期間仍跑舊版，同一 release 內停寫又收權限，會讓舊版的 INSERT 在空窗內 permission denied；ORM 的 `select(Model)` 會列出所有 mapped 欄位，所以「已停寫」不代表「已不讀」。

## Result

- 2026-10-05 18:23:55Z 切換完成（PR #125 的 `--waive-soak`；halt 4 分 32 秒，epoch 2＝ledger，resting offer 0，trading 自動恢復）。
- S1-8 上線：PR #126–#133；`legacy_archive` 13 張表（含 manifest）；restore gate 改驗 ledger 後實跑 0 mismatch。D6 的第一步（停寫）在 PR #133、第二步在 PR #134。
- S1-8 收尾（2026-10-07 前上線並在 prod 驗證）：D6 第三步與 JSON null 修正 PR #136；legacy 事件模型死碼與封存 ORM PR #137、#138；cid 移除與 submit port 拆分 PR #139；`backend/ARCHITECTURE.md` 改寫 PR #140。取捨見 Amendment (2026-10-07)。

## Followup

- 死碼清理 PR：`execution_quality.py`、沒有 producer 的 marketfeed payload 與 `emit_order_fill`、`LedgerVenueHintSink.offer_gone`、`allocate_gap` 路徑、仍寫「Dormant」的模組 docstring。
- `release_archive` 也不在 `alembic check` 比較範圍內，補上和 `legacy_archive` 相同的 head catalog 對照測試（Amendment 2026-10-07）。
- `/executions` API 與 frontend 型別裡的 `cid`：只有 `legacy_archive` 的列有值，隨封存一起退役（見 Revocation Triggers 第三條）。

## Revocation Triggers

- prod 加入第二個交易所帳戶：該 scope 沒有 seed，需要 admission 路徑，重新評估 D4 的寫入者檢查。
- 每個行程都 assert schema head：epoch 讀取與寫入者檢查可移除（schema head 已蘊含 ledger）。
- 不再需要在 History 顯示切換前資料：`legacy_archive` dump 到 offsite（前後比對 manifest digest）後 drop；先拆掉指進來的 FK。

## Related

- 修訂 [2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) D6／D7'''；[2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue](2026-10-03-simulation-runs-the-ledger-on-a-simulated-venue.md)（soak 門檻）；[2026-10-06-ledger-restore-verification-replaces-prefix-test](2026-10-06-ledger-restore-verification-replaces-prefix-test.md)（S1-8 的 DR 驗證決定）。
- 來源：2026-10-05～06 Will 與 coding agent 的討論；S1-8 四個決定的選項由獨立 subagent 起草（業界基準、選項、約束分類）後 Will 採用。實作 PR #120–#134。

## Amendment (2026-10-07)：S1-8 收尾與原 Followup 的差異

**約束**：沿用 Context，另加 `external` Bitfinex funding offer submit 不收 client id（[09-28](2026-09-28-ledger-journal-and-venue-mirror-replace-event-sourcing.md) Context）。

- **cid 直接刪除，不改以 attempt id 關聯**（原 Followup 寫的是「改以 attempt id 關聯」）。
  - 基準：client order id 作為冪等鍵，例如 [FIX ClOrdID(11)](https://www.onixs.biz/fix-dictionary/4.4/tagNum_11.html)、Binance `newClientOrderId`。venue 不收這個欄位，本地 cid 從來沒送出去，所以在 venue 側做不了冪等。
  - 可選做法：A 用 attempt id 重新產生 cid 做關聯；B 刪除。
  - 選 B：去重、復原與 UNKNOWN 比對已經由 attempt journal、`submission_attempt_journal.execution_decision_id` 的 UNIQUE、UNKNOWN 不重送與金額指紋負責。cid 不在任何 public 表、journal 或 `ledger_digest` 裡，留著只是多一個沒有人驗證的身分。gate 改成要求 executor 原樣回傳 gate 建立的 `ReservationRef`（decision id＋signal correlation id）；沒回傳、不一致或綁到別的 venue offer，都記成 durable UNKNOWN 並封鎖該幣別。
  - 代價：log 與 trace 裡不再有短 id，追查改用 decision id。
- **submit port 在 gate 拆成兩個**：上游是 `ExecutorPort.submit(ready, ctx)`，venue 側是 `VenueExecutorPort.submit(ready, ctx, *, reservation_ref)`，`reservation_ref` 必填。
  - 可選做法：維持單一 port 加 optional kwargs，或拆成兩個型別。單一 port 的問題是 live executor 沒收到 reference 時會自己造一個，身分檢查就被繞過。
  - 選拆分：「reference 只由 gate 建立」由型別保證（mypy 擋下 5 種違規寫法）。代價是多一個 Protocol。
- **`legacy_archive` 由 migration 擁有，不建 ORM**。
  - 可選做法：保留封存表的 ORM 讓 `alembic check` 比對；或從 `env.py` 的 include_name 排除，另外寫 catalog 快照測試。
  - 選後者：唯一讀者 webapi 走 Core Table。`test_no_later_migration_alters_the_archive` 用 `format_type`（保留長度、精度、陣列與 enum 細節）比對 head 的封存欄位與 `c2d3e4f5a6b7` 剛套用時是否相同。
  - 代價：封存結構的變更不再被 autogenerate 發現，只靠這個測試擋。
- **JSON 欄位只存 document 或 SQL NULL**（修根因）。
  - 原因：SQLAlchemy JSON 預設把 `None` 寫成 `'null'::jsonb`，`IS NULL` 判斷不到。prod 有 2 列 `seed_provenance` 因此讓切換後的 attempt 被當成 seeded。
  - 可選做法：在每個查詢補 `jsonb_typeof` 判斷；或在寫入端改用 `JSON_DOCUMENT`（`none_as_null=True`），並替每個 public JSON 欄位加 `CHECK (jsonb_typeof(col) <> 'null')`。
  - 選寫入端：一個型別加 DB 約束就涵蓋所有 consumer，不必依賴每個讀者都記得處理。`ck_submission_attempt_policy_or_seed` 同時收緊為「恰好一個」。
  - 代價：migration `a6c7e8f9b0d1` 改寫了 append-only 列的 bytes，restore drill 的目標時點若早於它，會回報預期中的 `ledger_digest_mismatch`（`docs/runbooks/offsite-dr.md`）。
- **重新評估條件**：Bitfinex funding submit 開始接受 client id 時，回到基準，以 attempt id 作為 client id 送出。
