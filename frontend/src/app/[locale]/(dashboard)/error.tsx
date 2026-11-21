"use client";

import { useTranslations } from "next-intl";
import { isDevelopment } from "@/lib/env";

export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const t = useTranslations("common");

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4">
      <h1 className="text-2xl font-bold">{t("error")}</h1>
      <p className="text-sm text-muted-foreground">
        {isDevelopment ? error.message : t("unexpectedError")}
      </p>
      <button
        type="button"
        onClick={reset}
        className="rounded bg-accent px-4 py-2 text-accent-foreground"
      >
        {t("tryAgain")}
      </button>
    </div>
  );
}
