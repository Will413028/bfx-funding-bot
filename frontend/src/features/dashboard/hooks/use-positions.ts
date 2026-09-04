import { useQuery } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { positionKeys } from "@/lib/query-keys";
import type { Position } from "@/types";

export function usePositions(exchangeAccountId?: string) {
  return useQuery({
    queryKey: positionKeys.list(exchangeAccountId),
    queryFn: () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.get<Position[]>(
        accountScopedPath(exchangeAccountId, "/positions"),
      );
    },
    enabled: Boolean(exchangeAccountId),
    staleTime: 30_000,
  });
}
