"use client";

import { LoadMoreButton } from "@/components/shared/load-more-button";
import { HistorySkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { BillingTable } from "@/features/history/components/billing-table";
import { ExecutionTable } from "@/features/history/components/execution-table";
import { useBilling } from "@/features/history/hooks/use-billing";
import { useExecutions } from "@/features/history/hooks/use-executions";

export default function HistoryPage() {
  const executions = useExecutions();
  const billing = useBilling();

  const executionRecords = executions.data?.pages.flatMap((p) => p.data) ?? [];
  const billingRecords = billing.data?.pages.flatMap((p) => p.data) ?? [];

  const isLoading = executions.isLoading || billing.isLoading;
  const isError = executions.isError || billing.isError;

  if (isLoading) {
    return <HistorySkeleton />;
  }

  if (isError) {
    return <QueryError message="Failed to load history" onRetry={() => { executions.refetch(); billing.refetch(); }} />;
  }

  return (
    <div className="space-y-6">
      <h1 className="font-semibold text-xl tracking-tight">History</h1>
      <Tabs defaultValue="executions">
        <TabsList>
          <TabsTrigger value="executions">Executions</TabsTrigger>
          <TabsTrigger value="billing">Billing</TabsTrigger>
        </TabsList>
        <TabsContent value="executions" className="mt-4">
          <ExecutionTable records={executionRecords} />
          <LoadMoreButton
            hasMore={executions.hasNextPage}
            isFetchingNextPage={executions.isFetchingNextPage}
            onClick={() => executions.fetchNextPage()}
          />
        </TabsContent>
        <TabsContent value="billing" className="mt-4">
          <BillingTable records={billingRecords} />
          <LoadMoreButton
            hasMore={billing.hasNextPage}
            isFetchingNextPage={billing.isFetchingNextPage}
            onClick={() => billing.fetchNextPage()}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
