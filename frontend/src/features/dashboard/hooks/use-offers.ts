import { useQuery } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { offerKeys } from "@/lib/query-keys";
import type { OfferClaim } from "@/types";

/**
 * Active offer claims. Backend defaults to pending/claimed when no
 * `state` filter is given.
 */
export function useOffers(exchangeAccountId?: string, state?: string) {
  return useQuery({
    queryKey: offerKeys.list(exchangeAccountId, state),
    queryFn: () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.get<OfferClaim[]>(
        accountScopedPath(exchangeAccountId, "/offers"),
        {
          params: state !== undefined ? { state } : undefined,
        },
      );
    },
    enabled: Boolean(exchangeAccountId),
    staleTime: 30_000,
  });
}
