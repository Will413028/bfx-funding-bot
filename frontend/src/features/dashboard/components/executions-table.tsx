"use client";

import { useTranslations } from "next-intl";
import { ExecutionEventsTable } from "@/components/shared/execution-events-table";
import { LoadMoreButton } from "@/components/shared/load-more-button";
import type { ExecutionEvent } from "@/types";

interface ExecutionsTableProps {
  events: ExecutionEvent[];
  hasMore: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
}

/** Dashboard "Recent Executions" card around the shared event_log table. */
export function ExecutionsTable({
  events,
  hasMore,
  isFetchingNextPage,
  onLoadMore,
}: ExecutionsTableProps) {
  const t = useTranslations("overview");

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("recentExecutions")}
      </h3>
      <div className="mt-4">
        <ExecutionEventsTable events={events} />
      </div>
      <LoadMoreButton
        hasMore={hasMore}
        isFetchingNextPage={isFetchingNextPage}
        onClick={onLoadMore}
      />
    </div>
  );
}
