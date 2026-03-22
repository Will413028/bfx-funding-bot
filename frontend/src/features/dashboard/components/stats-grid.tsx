"use client";

import { Activity, DollarSign, TrendingUp, Wallet } from "lucide-react";
import { useTranslations } from "next-intl";
import { StatCard } from "@/components/shared/stat-card";
import { formatUSD } from "@/lib/format";
import type { DashboardSummary, EarningsSummary } from "@/types";

/** Split USD string into integer + decimal for dimming */
function UsdValue({ amount }: { amount: number }) {
  const formatted = formatUSD(amount);
  const dotIndex = formatted.lastIndexOf(".");
  if (dotIndex === -1) return <>{formatted}</>;
  return (
    <>
      {formatted.slice(0, dotIndex)}
      <span className="text-zinc-500">{formatted.slice(dotIndex)}</span>
    </>
  );
}

interface StatsGridProps {
  dashboard: DashboardSummary;
  earnings: EarningsSummary;
}

export function StatsGrid({ dashboard, earnings }: StatsGridProps) {
  const t = useTranslations("overview");
  const ts = useTranslations("status");
  const totalLent = earnings.totalLent;
  const available = dashboard.wallet?.balanceAvailable ?? 0;
  const dailyEarning = earnings.estimatedDailyEarning;
  const engineRunning = dashboard.engineReady;

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        icon={Wallet}
        label={t("totalLent")}
        value={<UsdValue amount={totalLent} />}
        subtitle={t("activeCreditsCount", { count: earnings.activeCredits })}
      />
      <StatCard
        icon={DollarSign}
        label={t("availableBalance")}
        value={<UsdValue amount={available} />}
      />
      <StatCard
        icon={TrendingUp}
        label={t("estimatedDailyEarning")}
        value={<UsdValue amount={dailyEarning} />}
        subtitle={`APY ${(earnings.weightedAPY * 100).toFixed(2)}%`}
      />
      <StatCard
        icon={Activity}
        label={t("workerStatus")}
        value={engineRunning ? ts("running") : ts("stopped")}
        className={engineRunning ? "text-emerald-400" : "text-rose-500"}
      />
    </div>
  );
}
