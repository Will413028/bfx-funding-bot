import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { earningsKeys } from "@/lib/query-keys";
import type { DailyEarning } from "@/types";

export function useEarningsHistory(days = 30) {
  return useQuery({
    queryKey: earningsKeys.history(days),
    queryFn: () =>
      apiClient.get<DailyEarning[]>("/earnings/history", {
        params: { days: String(days) },
      }),
    staleTime: 5 * 60_000,
  });
}
