"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";

interface QueryErrorProps {
  message?: string;
  onRetry?: () => void;
}

export function QueryError({ message, onRetry }: QueryErrorProps) {
  const t = useTranslations("common");
  const displayMessage = message ?? t("loadFailed");

  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-white/5 bg-white/[0.01] py-12">
      <AlertTriangle className="size-8 text-zinc-600" />
      <p className="mt-3 text-sm text-zinc-500">{displayMessage}</p>
      {onRetry && (
        <Button variant="outline" size="sm" className="mt-4" onClick={onRetry}>
          <RefreshCw className="mr-1.5 size-3.5" />
          {t("tryAgain")}
        </Button>
      )}
    </div>
  );
}
