## 1. Backend: Appconfig 擴充

- [x] 1.1 在 appconfig.Config 新增 AxiomToken 和 AxiomDataset 欄位（optional）

## 2. Backend: Axiom zap core 整合

- [x] 2.1 go get github.com/axiomhq/axiom-go 安裝依賴
- [x] 2.2 改寫 newLogger：Tee 模式 (stdout + Axiom zap core)
- [x] 2.3 fx.Lifecycle OnStop flush Axiom buffer

## 3. Frontend: next-axiom 整合

- [x] 3.1 pnpm add next-axiom 安裝依賴
- [x] 3.2 next.config.ts 加入 withAxiom wrapper
- [x] 3.3 建立 src/lib/axiom.ts log utility

## 4. 文件更新

- [x] 4.1 更新 ROADMAP.md Phase H 改為 Axiom 方案
- [x] 4.2 更新 backend_architecture.md 技術棧表格
