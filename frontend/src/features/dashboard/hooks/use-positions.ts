import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { positionKeys } from "@/lib/query-keys";
import type { Position } from "@/types";

export function usePositions() {
  return useQuery({
    queryKey: positionKeys.list(),
    queryFn: () => apiClient.get<Position[]>("/positions"),
    staleTime: 30_000,
  });
}
