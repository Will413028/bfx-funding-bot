"use client";

import { StrategySkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { StrategyForm } from "@/features/strategy/components/strategy-form";
import { useConfig } from "@/features/strategy/hooks/use-config";

export default function StrategyPage() {
  const { data: userConfig, isLoading, isError, refetch } = useConfig();

  if (isLoading) {
    return <StrategySkeleton />;
  }

  if (isError) {
    return <QueryError message="Failed to load strategy config" onRetry={refetch} />;
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="font-semibold text-xl tracking-tight">Strategy Config</h1>
      <StrategyForm userConfig={userConfig ?? null} />
    </div>
  );
}
