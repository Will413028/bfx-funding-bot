import { useInfiniteQuery } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { executionKeys } from "@/lib/query-keys";
import type { ExecutionEventsResponse, ExecutionEventType } from "@/types";

export const EXECUTION_EVENTS_PAGE_SIZE = 25;

/**
 * Contract v2: the server pagination envelope drives the before-cursor —
 * `nextBefore` is the smallest event_seq of the page, `hasMore` replaces
 * the old "full page => more" heuristic.
 */
export function getNextEventsPageParam(
  lastPage: ExecutionEventsResponse,
): number | undefined {
  const { hasMore, nextBefore } = lastPage.pagination;
  return hasMore && nextBefore != null ? nextBefore : undefined;
}

interface UseExecutionEventsOptions {
  exchangeAccountId?: string;
  /** Restrict to a single event_log type; omit for all events. */
  eventType?: ExecutionEventType;
  pageSize?: number;
}

export function useExecutionEvents({
  exchangeAccountId,
  eventType,
  pageSize = EXECUTION_EVENTS_PAGE_SIZE,
}: UseExecutionEventsOptions = {}) {
  return useInfiniteQuery({
    queryKey: executionKeys.events(exchangeAccountId, eventType),
    queryFn: ({ pageParam }) => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.getList<ExecutionEventsResponse>(
        accountScopedPath(exchangeAccountId, "/executions"),
        {
          params: {
            limit: String(pageSize),
            ...(eventType !== undefined ? { event_type: eventType } : {}),
            ...(pageParam !== undefined ? { before: String(pageParam) } : {}),
          },
        },
      );
    },
    enabled: Boolean(exchangeAccountId),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: getNextEventsPageParam,
    staleTime: 60_000,
  });
}
