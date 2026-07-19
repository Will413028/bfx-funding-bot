import { Skeleton } from "@/components/ui/skeleton";

// Stable, non-index keys for static placeholder lists (biome noArrayIndexKey).
const placeholderKeys = (count: number, prefix: string) =>
  Array.from({ length: count }, (_, i) => `${prefix}-${i}`);

export function OverviewSkeleton() {
  return (
    <div className="space-y-4">
      {/* Positions + active offers */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Skeleton className="h-48 rounded-xl" />
        <Skeleton className="h-48 rounded-xl lg:col-span-2" />
      </div>
      {/* Recent executions */}
      <Skeleton className="h-72 rounded-xl" />
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
        {placeholderKeys(5, "strategy").map((key) => (
          <Skeleton key={key} className="h-14 rounded-lg" />
        ))}
      </div>
      <Skeleton className="h-10 w-full rounded-md" />
    </div>
  );
}

/**
 * Executions-browser table region only — the history page keeps its header
 * and filter chips mounted while a filter change refetches.
 */
export function HistorySkeleton() {
  return (
    <div className="space-y-2">
      {placeholderKeys(8, "history").map((key) => (
        <Skeleton key={key} className="h-12 rounded-lg" />
      ))}
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
