import { useQuery } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { attributionKeys } from "@/lib/query-keys";
import type { WeeklyAttributionPoint } from "@/types";

export function useWeeklyAttribution(exchangeAccountId?: string) {
  return useQuery({
    queryKey: attributionKeys.weekly(exchangeAccountId),
    queryFn: () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.get<WeeklyAttributionPoint[]>(
        accountScopedPath(exchangeAccountId, "/attribution/weekly"),
      );
    },
    enabled: Boolean(exchangeAccountId),
    staleTime: 60 * 60_000, // 資料每週更新，1h stale 足夠
  });
}
