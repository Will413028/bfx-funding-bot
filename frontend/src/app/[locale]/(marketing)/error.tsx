"use client";

import { useTranslations } from "next-intl";

export default function MarketingError({
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
        {process.env.NODE_ENV === "development"
          ? error.message
          : t("unexpectedError")}
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
