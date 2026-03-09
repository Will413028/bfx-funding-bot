## ADDED Requirements

### Requirement: Vercel 部署設定
專案根目錄 SHALL 包含 `vercel.json` 設定檔，定義部署行為。

#### Scenario: Vercel 部署
- **WHEN** Vercel 偵測到 push 並觸發部署
- **THEN** 使用 `vercel.json` 中的設定（headers 等）

### Requirement: env 驗證 fail-fast
`env.ts` 在 server 端 SHALL 於 Zod parse 失敗時立即終止程序（`process.exit(1)`），並輸出缺少的環境變數清單。

#### Scenario: 所有必要環境變數已設定
- **WHEN** 所有 Zod schema 定義的環境變數皆已正確設定
- **THEN** 應用程式正常啟動

#### Scenario: 缺少必要環境變數
- **WHEN** 任一必要環境變數缺失或格式錯誤
- **THEN** server 端啟動時 console.error 輸出缺少的變數名稱，並以 exit code 1 終止

#### Scenario: Client 端缺少 NEXT_PUBLIC 變數
- **WHEN** client 端 Zod parse 失敗
- **THEN** console.error 輸出警告訊息（不 exit，因為 browser 無法 exit）
