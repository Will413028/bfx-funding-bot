"use client";

import { useTranslations } from "next-intl";
import { QueryError } from "@/components/shared/query-error";
import { AttributionChart } from "@/features/attribution/components/attribution-chart";
import { useWeeklyAttribution } from "@/features/attribution/hooks/use-weekly-attribution";

export default function AttributionPage() {
  const t = useTranslations("attribution");
  const { data, isLoading, isError, refetch } = useWeeklyAttribution();

  if (isLoading)
    return <div className="p-6 text-sm text-zinc-500">{t("loading")}</div>;
  if (isError || !data)
    return <QueryError message={t("loadFailed")} onRetry={() => refetch()} />;

  const cells = [...new Set(data.map((p) => p.cell))].sort();
  return (
    <div className="space-y-4 p-6">
      <h1 className="text-lg font-semibold text-zinc-100">{t("title")}</h1>
      {cells.length === 0 && (
        <p className="text-sm text-zinc-500">{t("empty")}</p>
      )}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {cells.map((cell) => (
          <AttributionChart
            key={cell}
            cell={cell}
            points={data.filter((p) => p.cell === cell)}
          />
        ))}
      </div>
    </div>
  );
}
