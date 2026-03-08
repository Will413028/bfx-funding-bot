## 1. Interface 定義

- [x] 1.1 在 `repository/interfaces.go` 新增 SnapshotCache interface（Set/Get/Delete）
- [x] 1.2 在 `repository/interfaces.go` 新增 SnapshotPubSub interface（Publish/Subscribe/Close）

## 2. Redis 實作

- [x] 2.1 建立 `repository/redis/snapshot.go`：實作 SnapshotCache（JSON 序列化, key=`market:snapshot:{symbol}`）
- [x] 2.2 建立 `repository/redis/pubsub.go`：實作 SnapshotPubSub（channel=`market:snapshot:updates`）

## 3. DI 整合

- [x] 3.1 建立 `repository/redis/di.go`：fx.Module 提供 SnapshotCacheRepo + SnapshotPubSubRepo
- [x] 3.2 修改 `repository/di.go`：整合 Redis module + interface binding

## 4. 測試

- [x] 4.1 建立 `repository/redis/snapshot_test.go`：使用 miniredis 測試 Set/Get/Delete + TTL
- [x] 4.2 建立 `repository/redis/pubsub_test.go`：使用 miniredis 測試 Publish/Subscribe + malformed message handling

## 5. 驗證

- [x] 5.1 確認完整專案編譯通過 + 所有測試通過
