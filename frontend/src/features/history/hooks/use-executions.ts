import { useInfiniteQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { executionKeys } from "@/lib/query-keys";
import type { ExecutionListResponse } from "@/types";

const LIMIT = 20;

export function useExecutions() {
  return useInfiniteQuery({
    queryKey: executionKeys.list(),
    queryFn: ({ pageParam }) =>
      apiClient.getList<ExecutionListResponse>("/executions", {
        params: {
          limit: String(LIMIT),
          ...(pageParam ? { after: pageParam } : {}),
        },
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.pagination.hasMore ? lastPage.pagination.nextCursor : undefined,
  });
}
