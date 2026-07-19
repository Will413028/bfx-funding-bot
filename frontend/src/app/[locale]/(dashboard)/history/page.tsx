"use client";

import { useTranslations } from "next-intl";
import { ExecutionEventsTable } from "@/components/shared/execution-events-table";
import { LoadMoreButton } from "@/components/shared/load-more-button";
import { HistorySkeleton } from "@/components/shared/page-skeleton";
import { QueryError } from "@/components/shared/query-error";
import { useExecutionEvents } from "@/features/dashboard/hooks/use-execution-events";
import { EventTypeFilter } from "@/features/history/components/event-type-filter";
import { useEventTypeFilter } from "@/features/history/hooks/use-event-type-filter";

/** Full-page browser pages deeper than the dashboard card. */
const HISTORY_PAGE_SIZE = 50;

export default function HistoryPage() {
  const t = useTranslations("history");
  const [eventType, setEventType] = useEventTypeFilter();
  const executions = useExecutionEvents({
    eventType: eventType ?? undefined,
    pageSize: HISTORY_PAGE_SIZE,
  });

  const events = executions.data?.pages.flatMap((p) => p.data) ?? [];

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="font-semibold text-xl tracking-tight">{t("title")}</h1>
        <p className="text-sm text-zinc-500">{t("subtitle")}</p>
      </div>
      <EventTypeFilter value={eventType} onChange={setEventType} />
      {executions.isLoading ? (
        <HistorySkeleton />
      ) : executions.isError || !executions.data ? (
        <QueryError
          message={t("loadFailed")}
          onRetry={() => executions.refetch()}
        />
      ) : (
        <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
          <ExecutionEventsTable events={events} />
          <LoadMoreButton
            hasMore={executions.hasNextPage}
            isFetchingNextPage={executions.isFetchingNextPage}
            onClick={() => executions.fetchNextPage()}
          />
        </div>
      )}
    </div>
  );
}
