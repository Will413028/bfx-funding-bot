import { Skeleton } from "@/components/ui/skeleton";

export function OverviewSkeleton() {
  return (
    <div className="space-y-4">
      {/* Stats grid */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-xl" />
        ))}
      </div>
      {/* Charts */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Skeleton className="h-64 rounded-xl" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
      {/* Lists */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Skeleton className="h-48 rounded-xl" />
        <Skeleton className="h-48 rounded-xl" />
      </div>
    </div>
  );
}

export function ApiKeysSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <Skeleton className="h-7 w-24" />
        <Skeleton className="h-9 w-24 rounded-md" />
      </div>
      <Skeleton className="h-32 rounded-xl" />
    </div>
  );
}

export function StrategySkeleton() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Skeleton className="h-7 w-40" />
      <div className="space-y-4">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-14 rounded-lg" />
        ))}
      </div>
      <Skeleton className="h-10 w-full rounded-md" />
    </div>
  );
}

export function HistorySkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-7 w-20" />
      <Skeleton className="h-10 w-56 rounded-md" />
      <div className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-12 rounded-lg" />
        ))}
      </div>
    </div>
  );
}

export function SettingsSkeleton() {
  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <Skeleton className="h-7 w-24" />
      <Skeleton className="h-28 rounded-xl" />
      <Skeleton className="h-48 rounded-xl" />
    </div>
  );
}
