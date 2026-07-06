import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { attributionKeys } from "@/lib/query-keys";
import type { WeeklyAttributionPoint } from "@/types";

export function useWeeklyAttribution() {
  return useQuery({
    queryKey: attributionKeys.weekly(),
    queryFn: () =>
      apiClient.get<WeeklyAttributionPoint[]>("/attribution/weekly"),
    staleTime: 60 * 60_000, // 資料每週更新，1h stale 足夠
  });
}
