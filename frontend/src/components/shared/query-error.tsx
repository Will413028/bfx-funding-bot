"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

interface QueryErrorProps {
  message?: string;
  onRetry?: () => void;
}

export function QueryError({
  message = "Failed to load data",
  onRetry,
}: QueryErrorProps) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-white/5 bg-white/[0.01] py-12">
      <AlertTriangle className="size-8 text-zinc-600" />
      <p className="mt-3 text-sm text-zinc-500">{message}</p>
      {onRetry && (
        <Button
          variant="outline"
          size="sm"
          className="mt-4"
          onClick={onRetry}
        >
          <RefreshCw className="mr-1.5 size-3.5" />
          Try again
        </Button>
      )}
    </div>
  );
}
