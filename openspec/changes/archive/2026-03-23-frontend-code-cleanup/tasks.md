## 1. 移除未使用的型別

- [x] 1.1 從 `types/index.ts` 移除 `PlanFeatures`、`EngineStatus`、`LendingStatus`（確認無 import）

## 2. 清理未使用的檔案與 exports

- [x] 2.1 確認 `lib/axiom.ts` 不存在（已被清理）
- [x] 2.2 從 `i18n/navigation.ts` 移除未使用的 `redirect` 和 `getPathname` exports

## 3. 抽取共用常數

- [x] 3.1 評估後：`use-billing.ts` 和 `use-executions.ts` 各自的 `const LIMIT = 20` 是合理的獨立設定，共用常數反而增加不必要的耦合。跳過。

## 4. 移除未使用依賴

- [x] 4.1 確認 `tailwindcss-animate` 實際被 `globals.css` 使用（`@plugin "tailwindcss-animate"`），不應移除

## 5. 驗證

- [x] 5.1 `biome check` 通過
