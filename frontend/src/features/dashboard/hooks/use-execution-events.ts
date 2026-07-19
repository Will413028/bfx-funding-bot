import { useInfiniteQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { executionKeys } from "@/lib/query-keys";
import type { ExecutionEvent } from "@/types";

export const EXECUTION_EVENTS_PAGE_SIZE = 25;

/**
 * Cursor for the next page: the smallest event_seq seen so far (rows come
 * back event_seq descending). A short page means the log is exhausted.
 */
export function getNextEventsPageParam(
  lastPage: ExecutionEvent[],
): number | undefined {
  if (lastPage.length < EXECUTION_EVENTS_PAGE_SIZE) return undefined;
  return lastPage[lastPage.length - 1]?.eventSeq;
}

export function useExecutionEvents() {
  return useInfiniteQuery({
    queryKey: executionKeys.events(),
    queryFn: ({ pageParam }) =>
      apiClient.get<ExecutionEvent[]>("/executions", {
        params: {
          limit: String(EXECUTION_EVENTS_PAGE_SIZE),
          ...(pageParam !== undefined ? { before: String(pageParam) } : {}),
        },
      }),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: getNextEventsPageParam,
    staleTime: 60_000,
  });
}
