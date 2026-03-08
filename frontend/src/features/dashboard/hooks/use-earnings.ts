import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { earningsKeys } from "@/lib/query-keys";
import type { EarningsSummary } from "@/types";

export function useEarnings() {
  return useQuery({
    queryKey: earningsKeys.summary(),
    queryFn: () => apiClient.get<EarningsSummary>("/earnings"),
    staleTime: 60_000,
  });
}
