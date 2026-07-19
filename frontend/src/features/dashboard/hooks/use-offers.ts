import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { offerKeys } from "@/lib/query-keys";
import type { OfferClaim } from "@/types";

/**
 * Active offer claims. Backend defaults to pending/claimed when no
 * `state` filter is given.
 */
export function useOffers(state?: string) {
  return useQuery({
    queryKey: offerKeys.list(state),
    queryFn: () =>
      apiClient.get<OfferClaim[]>("/offers", {
        params: state !== undefined ? { state } : undefined,
      }),
    staleTime: 30_000,
  });
}
