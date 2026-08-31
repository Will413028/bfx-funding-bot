"use client";

import { useTranslations } from "next-intl";
import { OverviewSkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { useSelectedExchangeAccountId } from "@/features/accounts/hooks/use-exchange-accounts";
import { useApiKeys } from "@/features/api-keys/hooks/use-api-keys";
import { ExecutionsTable } from "@/features/dashboard/components/executions-table";
import { OffersTable } from "@/features/dashboard/components/offers-table";
import { PositionsCard } from "@/features/dashboard/components/positions-card";
import { SetupChecklist } from "@/features/dashboard/components/setup-checklist";
import { useExecutionEvents } from "@/features/dashboard/hooks/use-execution-events";
import { useOffers } from "@/features/dashboard/hooks/use-offers";
import { usePositions } from "@/features/dashboard/hooks/use-positions";
import { useConfig } from "@/features/strategy/hooks/use-config";

export default function OverviewPage() {
  const t = useTranslations("overview");
  const account = useSelectedExchangeAccountId();
  const positions = usePositions(account.exchangeAccountId);
  const offers = useOffers(account.exchangeAccountId);
  const executions = useExecutionEvents({
    exchangeAccountId: account.exchangeAccountId,
  });
  const { data: apiKeys } = useApiKeys(account.exchangeAccountId);
  const { data: config } = useConfig(account.exchangeAccountId);

  if (
    account.isLoading ||
    positions.isLoading ||
    offers.isLoading ||
    executions.isLoading
  ) {
    return <OverviewSkeleton />;
  }

  if (
    account.isError ||
    !account.exchangeAccountId ||
    positions.isError ||
    offers.isError ||
    executions.isError ||
    !positions.data ||
    !offers.data ||
    !executions.data
  ) {
    return (
      <QueryError
        message={t("loadFailed")}
        onRetry={() => {
          positions.refetch();
          offers.refetch();
          executions.refetch();
        }}
      />
    );
  }

  const hasVerifiedKey =
    Array.isArray(apiKeys) && apiKeys.some((k) => k.status === "verified");
  const hasStrategy = config != null;
  const setupComplete = hasVerifiedKey && hasStrategy;

  const events = executions.data.pages.flatMap((p) => p.data);

  return (
    <div className="space-y-4">
      {!setupComplete && (
        <SetupChecklist
          hasVerifiedKey={hasVerifiedKey}
          hasStrategy={hasStrategy}
        />
      )}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <PositionsCard positions={positions.data} />
        <div className="lg:col-span-2">
          <OffersTable offers={offers.data} />
        </div>
      </div>
      <ExecutionsTable
        events={events}
        hasMore={executions.hasNextPage}
        isFetchingNextPage={executions.isFetchingNextPage}
        onLoadMore={() => executions.fetchNextPage()}
      />
    </div>
  );
}
