import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { dashboardKeys } from "@/lib/query-keys";
import type { DashboardSummary } from "@/types";

export function useDashboard() {
  return useQuery({
    queryKey: dashboardKeys.summary(),
    queryFn: () => apiClient.get<DashboardSummary>("/dashboard"),
    staleTime: 60_000,
  });
}
