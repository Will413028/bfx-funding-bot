---
title: 動態 CapitalPolicy 與 release-scoped canary 分離
date: 2026-09-19
status: active
supersedes: "[2026-05-30-balance-aware-cap-gate](2026-05-30-balance-aware-cap-gate.md)"
tags: [bfx-funding-bot, decision, safety, allocation, deployment]
related-commits:
  - eeb49a4
  - db03a22
  - ca7e3e6
  - b6fb2a0
  - af1e735
  - 79d851f
  - a8c0f47
  - a9949ce
  - 6709335
---

# 動態 CapitalPolicy 與 release-scoped canary 分離

## Context

使用者要「有多少合格可用資金就放多少；未設定就不保留」，不再手動同步固定總額 cap。舊 profile 混用長期 canary、單次驗證額度、兩個 normal cells 與 allocation scalar；部署又在 VM rebuild，與預先填寫 image digest 衝突。

本決策取代舊 balance-aware ADR 的固定 env buffer、in-memory 資金 authority 與 cap 綁定方式；保留 action-size clamp、幣別原生單位、帳戶隔離與持久化 halt。已有真錢歷史，不能清空帳務換取簡化。

## Options Considered

- **A. 維持固定 cap、同步全部來源**：限制明確、改動少；入金後仍需手調，release 與正常額度繼續耦合。
- **B. 只依 venue available／拒單結果**：最簡單；無法證明本機並行命令、重啟 pending commitments 與 stale snapshot 不重用同一餘額。
- **C. 單一動態 CapitalPolicy＋獨立 ReleaseCanary（採用）**：資金隨 fresh snapshot 變動，release 只約束自己的單次命令；代價是持久化證據、明確 migration 與更多驗證。

Artifact authority 的子選擇（controller依自主授權裁定）：

- **Trusted-host receipt（採用）**：由現有部署主機 inspect 實際 container，產生受保護唯讀證明；沿用主機信任邊界，不能防 host-root compromise。
- **Signed attestation**：以獨立簽章與 pinned authority 分離產製／驗證信任；代價是新增 key distribution、rotation 與驗證契約，現有單 VM 流程尚無此基礎。

Funding minimum 的子選擇（controller依自主授權裁定，實作／複審待完成）：

- **官方USD規則＋venue public FX（採用）**：以有來源的minimum／precision與即時FX作本機等值驗證；不假設USDT parity，但未證明FX等於funding引擎內部估值。
- **等待funding-specific換算證明，fUST持續failclosed**：避免此推論；目前文件未提供對應契約，需要外部查證，無法僅靠現有API文件完成。固定native150不是等價替代。

## Decision

- **D1＝spec §3／§8**：applied policy 按 account/environment/symbol 管理，`all_available`、初始 reserve0、既有 max_cell_fraction0.70；fUST enabled，fUSD disabled。缺 policy／非法值 block，draft 不生效；不保留 env/YAML 金額 fallback。
- **D2＝spec §4**：共用純 evaluator：spendable=max(0,A−L−R)，cell_limit=max(0,T−R)×fraction，新單不超 spendable 與 cell_headroom 的較小值。A是available，L是未證明反映的durable commitments，T按canonical分類不重加reservation。
- **D3＝spec §4／db03a22**：intent、policy revision、snapshot evidence 在同一account lock／DB transaction 授權寫入，commit後才transport。snapshot需prefetch fence＋兩次完整穩定觀測；缺provenance的已知credits保守計入每cell exposure、在T只計一次，不猜ownership、不掩蓋UNKNOWN。
- **D4＝spec §6**：normal mode為paper/shadow/live；release一次性canary綁artifact/config/policy/schema/projector/account/halt epoch、明確max_amount與expiry。durable consume後不重試；ACK及兩次完整reconcile才可validated，validated不自動resume。
- **D5＝spec §7／6709335 amendment**：build once、部署同一artifact；v2明列config digest、OCI manifest digest與實際host image ID，backend／frontend分別export並驗完整內容鏈。從受驗manifest解析host identity，不讓使用者手填digest、不在deploy pull moving main／重建；v1 failclosed。
- **D6＝spec §5／§6／§9**：保留halt、UNKNOWN、writer、audit、book／loss guards與本次DR RPO≤300s／RTO≤3600s。fUSD disabled不是縮減full-account observation coverage；此點更新06-01 ADR的09-12 amendment。
- **D7＝Task4 ruling／a8c0f47**：部署主機是launch authority；runtime核對protected readonly receipt、實際啟動關聯與code/config/Python inventory，不接受caller hash。每container有新launch identity；stable release/config/policy identity才可跨同版本重啟沿用promotion。
- **D8＝final-review修正裁決**：adapter版本化官方USD150 minimum／8位amount precision，以Bitfinex public UST→USD FX做本機等值驗證；request開始時計齡，沿用30秒book bound作本機evidence上限，並非venue freshness承諾。缺失／非法／過期／rule不符即block；送出前重驗，不默增exact amount／越過headroom或session max。動態FX是evidence，rule算法／來源變更才改executable identity。

## Rationale

- **D1／D2**：相較A，移除無效或需手動追隨入金的固定來源；相較B，本機承諾仍有確定的扣抵邊界。代價是帳戶全部合格資金可能參與放貸，沒有固定最大損失保證；政策轉換必須揭露exposure差異，不能假稱等價。
- **D3**：時間較新不證明餘額已含某筆命令；交易所自主成交也不受本機lock限制。代價是兩次讀取與觀測期間暫停新單，unattributed credits可能保守降低利用率；不宣稱REST具有原子快照。
- **D4**：單次release風險額度不應永久限制正常帳戶；代價是需要狀態機、fresh evidence與authenticated promotion。一次canary只驗execution path，不證明策略收益或所有故障情境。
- **D5**：相較VM重建，可核對artifact才是共同核准對象。實際classic→containerd匯入證明ID namespace不同、pair export的OCI index只含backend，因此採v2與分開export，而非切換全域Docker store（會影響既有workloads／volumes）；代價是metadata／receipt破壞性版本更新、完整archive驗證與兩端實測。不新增registry／硬體，也不修改舊bundle或信任tag繞過binding。
- **D6**：資金政策重構不是放寬恢復／執行安全的理由；代價是外部狀態未證明時仍可能延後放貸。績效量測與既有G3證據保留，全面績效／現金流重設不夾帶本輪。
- **D7**：相較新增簽章基礎設施，沿用既有單VM受信任主機，不引入key或服務；代價是不能防主機root失陷，且全量hash可能增加鎖等待，須量測。runtime沒有Docker socket；缺少／過期／不符的receipt仍拒絕。producer與實際app startup不能由consumer fixture代替驗收。
- **D8**：相較等待未公開的funding引擎估值契約，採可驗證的官方輸入並明示推論，不把未知藏成native150 fallback；代價是內部估值／時點差異仍可能導致venue拒單且一次性permit已消耗。拒單持久化、不得自動重試；這不是保證最小單成功，也不授權agent真錢操作。

## Result

設計已核准，**技術部署完成，放貸驗收未完成**。取回變更：repo內 `git log --oneline eeb49a4^..cae3ffc`。Task1–5、整體修正與兩項實際部署缺陷均完成獨立review；部署source為ddef4e5，local main另含純runbook補正cae3ffc。最新exact-tree full3598passed／490deselected／4既有warnings181.27s，mypy222／ruff通過。未push；保留使用者原改動。

新pair在VM驗證／解析成功，schema b4e6f8a0c203、bootstrap與兩政策revision1明確套用。完整isolated restore為PostgreSQL18.6、RTO205s，event/replay與兩份archives吻合且資源清除。部署回報technical_start_only，公開網站200、API ready200、未授權401／公開JWT404；halt7／permit0／session0不變。指定帳號仍user／TOTP未啟用，未改auth flags。一般R2 preflight最新RPO1s；DR測量的900s lease已過，正式啟用前須刷新，不以歷史成功代替當下授權。

## Amendment (2026-09-22)：D5 的 build-once 改為 VM 上 build

### Amendment Context

D5 訂下「build once、部署同一 artifact、不在 deploy 重建」之後，實測一次 release 迭代約 **30 分鐘，其中約 20 分鐘是傳輸**（每次 275MB 的 OCI artifact）。Pending 原本把降低成本拆成三個選項（`rsync -c`、OCI 增量複製、放寬 verifier 接受壓縮 layer）都在「維持 build once」的前提下打轉。

2026-09-22 Will 提出第四條路：直接在 VM 上 build。這在 D5 的原始 Context 裡正是被推翻的那個做法——原句是「部署又在 VM rebuild，與預先填寫 image digest 衝突」。也就是說本次是**明知與三天前的裁定相反而重新裁定**，不是遺漏。

判斷情境已變的地方：現階段 production 仍 halted、canary 未跑過、**沒有真錢在線**，此時改動供應鏈的代價低於上線後；而 D5 那套跨主機 artifact 驗證的價值，至今也還沒有被任何一次真實事故證明過。

### Amendment Options

- **A. 維持 D5（build once ＋ 跨主機 artifact identity 鏈）**：供應鏈驗證最完整、可在任意主機重驗同一份 artifact；代價是每次迭代 30 分鐘，其中傳輸佔約 20 分鐘，且 metadata／receipt 的破壞性版本更新要兩端實測。
- **B. 只換傳輸方式（`rsync -c`）**：零成本、完全不動 identity 鏈，可立刻省下每次 275MB；代價是只解傳輸那一段，build 本身與「預先填寫 digest」的摩擦原樣保留。
- **C. VM 上 build（採用）**：迭代成本最低、不再有跨主機傳輸與預填 digest；代價是放棄「受驗 manifest → 實際跑的 image」這條**跨主機可重驗**的確定對應，改為只能在該 VM 上就地量得。

### Amendment Decision（D5'，amends D5）

- **D5'**：採 C。release 改為**在部署 VM 上以 clean tracked source build**，不再 `docker save` 匯出 OCI artifact、不再跨主機傳輸、不再預先填寫 image digest；`immutable_release.py` 的 `never build/pull` 不變式與 `image_artifact.py` 的跨主機 artifact 驗證隨之退場。
- **保留不動**：D4 的 release canary 綁定（artifact／config／policy／schema／projector／account／halt epoch——⚠️ 其中 **halt epoch 已於同日稍後被 [2026-09-22-maintenance-halt-without-canary](2026-09-22-maintenance-halt-without-canary.md) 推翻**，其餘仍成立）、D7 的 launch receipt 與 runtime code／config／Python inventory 核對。這兩者核對的是**本機實際啟動的 container 與檔案**，不依賴 build 發生在哪裡，因此 VM build 後仍成立——綁定對象由「預填的 digest」改為「build 完就地量得的 image ID ＋ config digest」。
- **明確不含**：Will 原話有「不需要紀錄 image 版本」一句。**連就地量得的 image identity 也拿掉，會直接讓 D4 的 canary 綁定與 D7 的 receipt 核對失去對象**，那是另一次裁定，本 Amendment 不涵蓋；在另行裁定前，identity 仍然要記，只是不再預填、不再跨主機驗。

### Amendment Rationale

- **D5' 選 C 而非 A**：A 的核心價值是「可核對的 artifact 才是共同核准對象」，而那個「共同」在現況不存在——單一 operator、單一 VM、無第二方需要獨立重驗。為一個目前沒有對象的性質，每次 release 付 20 分鐘傳輸，在還沒上線的階段不划算。
- **D5' 選 C 而非 B**：B 便宜且安全，是本來的建議方向；但它只動傳輸那一段，`build once` 帶來的預填 digest 與 metadata／receipt 版本管理仍在，Will 要的是整條摩擦消失而不是縮短其中一段。這是明知較保守選項存在而選擇較激進者，理由是上述情境判斷，不是沒看到 B。
- **放棄了什麼（要誠實記著）**：① 同一份 artifact 可在任意主機重驗的能力；② 「這顆 image 是從哪份 tracked source 出來的」這條原本由傳同一個 artifact 保證的鏈，改為只能靠 VM 上的 build 紀錄，**主機本身成為單點信任**（D7 原本就寫明 host root 失陷不在防護範圍，這次把依賴加深）；③ D5 v2 那次 classic→containerd 匯入實測與 pair export 的工程投入作廢。
- **與 D7 的關係**：D7 已把部署主機定為 launch authority，本次只是把 build 也移進同一個信任邊界，方向一致、不是新開一個信任假設。

### Amendment Revocation Triggers

- 出現第二台部署主機、或需要第二方獨立重驗同一份 release。
- canary 進真錢後發生任何一次「跑的不是我以為的那份 code／config」類事故。
- repo 轉為對外提供服務（非自用），供應鏈可稽核性重新成為外部要求。

## Followup

- Disabled fUSD僅配置、不補造零餘額，完整account coverage維持（2026-09-20 `ddef4e5`：bootstrap 只驗 enabled fUST，conversion 對 disabled cell 只記 `{status: disabled, capital_evaluated: false}`；不選「為建 wallet 而入金／轉帳」「缺 wallet 當 0」「disabled 幣別吞掉 snapshot 錯誤」——前者是不必要的金流動作，後兩者把缺席當證據；來源 spec／plan `2026-09-20-disabled-currency-bootstrap-design.md`、`2026-09-20-disabled-currency-bootstrap.md`，原文已不在 repo，本 ADR 即紀錄）。DR archive同bytes／同pin私有副本與webapi schema-version最小讀權限已實測，不擴大runtime權限。
- 正式human release前刷新同artifact的有效DR／preflight證據；不可改timestamp或放寬900s有效期。保留halt直到符合human activation程序。
- 配妥唯一operator與本人TOTP驗證；agent不代執行真錢canary、permit consumption、promotion或resume。技術服務健康與開始放貸分開驗收。

## Revocation Triggers

- 若無法提供穩定完整觀測／commitment分類證據，停止新增命令，重評authority設計，不回退舊cap或推測餘額。
- 若要啟用新幣別、提高concentration或支援外部實盤，另行評估授權、隔離與資金風險，不沿用本次fUST-only核准。
- 若部署主機不再受信任或要跨不同信任領域驗證artifact，重新設計attestation邊界，不能把現有host receipt當成獨立簽章證明。
- 若官方minimum／precision改變、FX語意與funding估值矛盾或持續minimum拒單，停止新增命令並重評D8；不能擅自增加amount或放寬freshness。
  - ⚠️ **2026-09-23 已觸發並重評**：持續拒單 2/2，D8 的「不增額」由 [2026-09-23-canary-submit-margin-over-venue-floor](2026-09-23-canary-submit-margin-over-venue-floor.md) 修訂為送單帶 0.5% 餘裕（規則本身不變）。

## Review Notes

依Followup完成狀態與production實測回訪；不以測試數或設計核准替代部署證據。

## Related

- **來源**：spec `2026-09-19-capital-policy-and-release-canary-design.md`；plan `2026-09-19-capital-policy-release.md`（原文已不在 repo，本 ADR 即紀錄）。
- **D7落地查核**：repo內 `backend_py/src/bfx_funding_bot/core/release_identity.py`（舊 commit hash 已隨 repo 公開（2026-09-27）改寫歷史而失效）；未保留簽章方案的理由與代價已寫在本ADR，不依賴暫存report存活。
- **D5 amendment來源**：spec `2026-09-19-release-image-portability-design.md` 與 plan `2026-09-19-release-image-portability.md`（原文已不在 repo，本 ADR 即紀錄）；這是合併後target反例觸發的新計畫，不重開已關閉的review。D5／D5' 已由 [2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md) 取代，portability 的 v2 metadata／receipt 隨之退場。
- **D8官方來源（2026-09-19核對）**：[Funding minimum](https://support.bitfinex.com/hc/en-us/articles/213918949-What-is-the-minimum-offer-for-Funding)、[Public FX](https://docs.bitfinex.com/reference/rest-public-foreign-exchange-rate)、[Amount precision](https://docs.bitfinex.com/docs/introduction)。來源分別支持USD等值門檻、CURRENT_RATE與8位精度，不宣稱三者構成funding內部估值保證；D8是本輪controller裁決。
- 被取代：[2026-05-30-balance-aware-cap-gate](2026-05-30-balance-aware-cap-gate.md)；保留幣別隔離並更新D3：[2026-06-01-per-currency-allocation-phase-2](2026-06-01-per-currency-allocation-phase-2.md)。
- 延續：[2026-07-27-halt-control-and-behaviour-observability](2026-07-27-halt-control-and-behaviour-observability.md)、[2026-08-31-account-isolated-execution-target-architecture](2026-08-31-account-isolated-execution-target-architecture.md)、[2026-09-04-offsite-dr-cloudflare-r2](2026-09-04-offsite-dr-cloudflare-r2.md)。D4 的一次性 canary 前身是 08-31 Halt 2 plan 的 bounded canary（一帳戶／一幣別／最小額、durable outcome＋兩次 full reconcile、不得 auto-ramp；`2026-08-31-halt2-canary-runbook.md`，原文已不在 repo，本 ADR 即紀錄）。D3 的讀取成本另由 [2026-09-20-capital-authority-bounded-read-by-prefix-hash](2026-09-20-capital-authority-bounded-read-by-prefix-hash.md) 改為有界。
- 技術pitfalls／測試過程不在本ADR；本ADR只壓縮選擇。相關通用教訓：projection completeness（cache不是真實來源）、cancel guard缺口（UNKNOWN不得默認放行），不把bug修正另造決策。
- 2026-09-25 採用 [2026-09-25-automated-probation-replaces-release-ceremony](2026-09-25-automated-probation-replaces-release-ceremony.md)：以變更分級＋執行期限額取代每個 build 的人工 ceremony，DR 與發版脫鉤。
- 2026-09-25 採用 [2026-09-25-ci-registry-digest-deploy](2026-09-25-ci-registry-digest-deploy.md)：以 CI build＋registry digest 部署取代 D5／D5' 的 artifact 路徑與 D7 launch receipt。
