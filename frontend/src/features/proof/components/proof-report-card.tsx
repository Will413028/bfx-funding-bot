"use client";

import { useTranslations } from "next-intl";
import type { ProofStats } from "@/features/proof/lib/compute-proof-stats";
import { formatPercent } from "@/lib/format";

const WINDOW_WEEKS = 12;

function StatTile({
  label,
  value,
  valueClassName = "text-zinc-100",
}: {
  label: string;
  value: string;
  valueClassName?: string;
}) {
  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <dt className="text-xs font-medium uppercase tracking-wider text-zinc-500">
        {label}
      </dt>
      <dd
        className={`mt-2 font-semibold text-2xl tabular-nums tracking-tight ${valueClassName}`}
      >
        {value}
      </dd>
    </div>
  );
}

export function ProofReportCard({ stats }: { stats: ProofStats }) {
  const t = useTranslations("proof");

  const spreadClassName =
    stats.spreadPct === null
      ? "text-zinc-100"
      : stats.spreadPct >= 0
        ? "text-emerald-400"
        : "text-rose-500";

  return (
    <dl className="grid grid-cols-1 gap-4 sm:grid-cols-3">
      <StatTile
        label={t("statAvgRealized", { weeks: WINDOW_WEEKS })}
        value={
          stats.avgRealizedAprPct === null
            ? "—"
            : formatPercent(stats.avgRealizedAprPct)
        }
        valueClassName="text-emerald-400"
      />
      <StatTile
        label={t("statSpread")}
        value={
          stats.spreadPct === null
            ? "—"
            : `${stats.spreadPct >= 0 ? "+" : ""}${formatPercent(stats.spreadPct)}`
        }
        valueClassName={spreadClassName}
      />
      <StatTile
        label={t("statWeeksTracked")}
        value={String(stats.weeksTracked)}
      />
    </dl>
  );
}
