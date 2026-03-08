import { useInfiniteQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { billingKeys } from "@/lib/query-keys";
import type { BillingListResponse } from "@/types";

const LIMIT = 20;

export function useBilling() {
  return useInfiniteQuery({
    queryKey: billingKeys.list(),
    queryFn: ({ pageParam }) =>
      apiClient.getList<BillingListResponse>("/billing", {
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
