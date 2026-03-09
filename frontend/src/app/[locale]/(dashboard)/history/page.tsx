"use client";

import { LoadMoreButton } from "@/components/shared/load-more-button";
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

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="size-6 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-400" />
      </div>
    );
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
