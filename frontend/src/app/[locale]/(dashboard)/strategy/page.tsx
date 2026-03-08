"use client";

import { StrategyForm } from "@/features/strategy/components/strategy-form";
import { useConfig } from "@/features/strategy/hooks/use-config";

export default function StrategyPage() {
  const { data: userConfig, isLoading, isError } = useConfig();

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-400" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-zinc-500">Failed to load strategy config</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="font-semibold text-xl tracking-tight">Strategy Config</h1>
      <StrategyForm userConfig={userConfig ?? null} />
    </div>
  );
}
