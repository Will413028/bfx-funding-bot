## Why

前端程式碼有幾個 code quality 問題：

1. **未使用的型別** — `types/index.ts` 匯出了 `PlanFeatures`、`EngineStatus`、`LendingStatus`，但從未被 import
2. **未使用的檔案** — `lib/axiom.ts` 存在但未被任何模組引用
3. **Magic numbers** — pagination limit `20` 在 `use-billing.ts` 和 `use-executions.ts` 中重複硬編碼
4. **未使用的依賴** — `tailwindcss-animate` 在 package.json 但未被使用
5. **未使用的 i18n exports** — `i18n/navigation.ts` 匯出 `redirect` 和 `getPathname` 但未被使用

## What Changes

- 移除未使用的型別定義
- 移除或整合未使用的檔案
- 抽取共用常數 `PAGINATION_LIMIT`
- 移除未使用的依賴
- 清理未使用的 exports

## Capabilities

### New Capabilities

（無——純清理）

### Modified Capabilities

（無行為變更）

## Impact

- `frontend/src/types/index.ts` — 移除未使用型別
- `frontend/src/lib/axiom.ts` — 移除或整合
- `frontend/src/lib/constants.ts` — 新增共用常數
- `frontend/src/features/history/hooks/use-billing.ts` — 使用常數
- `frontend/src/features/history/hooks/use-executions.ts` — 使用常數
- `frontend/src/i18n/navigation.ts` — 移除未使用 exports
- `frontend/package.json` — 移除未使用依賴
