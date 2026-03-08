package redis

import "go.uber.org/fx"

var Module = fx.Module("repository.redis",
	fx.Provide(NewSnapshotCacheRepo),
	fx.Provide(NewSnapshotPubSubRepo),
)
