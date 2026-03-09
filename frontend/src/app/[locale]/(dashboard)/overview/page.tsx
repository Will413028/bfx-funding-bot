"use client";

import { ChartPlaceholder } from "@/features/dashboard/components/chart-placeholder";
import { MarketPanel } from "@/features/dashboard/components/market-panel";
import { OffersList } from "@/features/dashboard/components/offers-list";
import { StatsGrid } from "@/features/dashboard/components/stats-grid";
import { useDashboard } from "@/features/dashboard/hooks/use-dashboard";
import { useDashboardWS } from "@/features/dashboard/hooks/use-dashboard-ws";
import { useEarnings } from "@/features/dashboard/hooks/use-earnings";

export default function OverviewPage() {
  const { data: dashboard, isLoading: dashLoading } = useDashboard();
  const { data: earnings, isLoading: earnLoading } = useEarnings();
  const { status: wsStatus } = useDashboardWS();

  if (dashLoading || earnLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-400" />
      </div>
    );
  }

  if (!dashboard || !earnings) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-zinc-500">Failed to load dashboard data</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <StatsGrid dashboard={dashboard} earnings={earnings} />
      <ChartPlaceholder />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <OffersList offers={dashboard.offers} />
        <MarketPanel market={dashboard.market} wsStatus={wsStatus} />
      </div>
    </div>
  );
}
