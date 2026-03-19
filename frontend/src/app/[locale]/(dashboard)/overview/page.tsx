"use client";

import { OverviewSkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { useApiKeys } from "@/features/api-keys/hooks/use-api-keys";
import { EarningsChart } from "@/features/dashboard/components/earnings-chart";
import { MarketPanel } from "@/features/dashboard/components/market-panel";
import { OffersList } from "@/features/dashboard/components/offers-list";
import { RateChart } from "@/features/dashboard/components/rate-chart";
import { SetupChecklist } from "@/features/dashboard/components/setup-checklist";
import { StatsGrid } from "@/features/dashboard/components/stats-grid";
import { useDashboard } from "@/features/dashboard/hooks/use-dashboard";
import { useDashboardWS } from "@/features/dashboard/hooks/use-dashboard-ws";
import { useEarnings } from "@/features/dashboard/hooks/use-earnings";
import { useConfig } from "@/features/strategy/hooks/use-config";

export default function OverviewPage() {
  const {
    data: dashboard,
    isLoading: dashLoading,
    isError: dashError,
    refetch: dashRefetch,
  } = useDashboard();
  const {
    data: earnings,
    isLoading: earnLoading,
    isError: earnError,
    refetch: earnRefetch,
  } = useEarnings();
  const { status: wsStatus } = useDashboardWS();
  const { data: apiKeys } = useApiKeys();
  const { data: config } = useConfig();

  if (dashLoading || earnLoading) {
    return <OverviewSkeleton />;
  }

  if (dashError || earnError || !dashboard || !earnings) {
    return (
      <QueryError
        message="Failed to load dashboard data"
        onRetry={() => {
          dashRefetch();
          earnRefetch();
        }}
      />
    );
  }

  const hasVerifiedKey =
    Array.isArray(apiKeys) &&
    apiKeys.some((k) => k.exchangeStatus === "verified");
  const hasStrategy = config != null;
  const engineReady = dashboard.engineReady;
  const setupComplete = hasVerifiedKey && hasStrategy && engineReady;

  return (
    <div className="space-y-4">
      {!setupComplete && (
        <SetupChecklist
          hasVerifiedKey={hasVerifiedKey}
          hasStrategy={hasStrategy}
          engineReady={engineReady}
        />
      )}
      <StatsGrid dashboard={dashboard} earnings={earnings} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <RateChart />
        <EarningsChart />
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <OffersList offers={dashboard.offers} />
        <MarketPanel market={dashboard.market} wsStatus={wsStatus} />
      </div>
    </div>
  );
}
