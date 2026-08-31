import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { exchangeAccountKeys } from "@/lib/query-keys";
import type { ExchangeAccount } from "@/types";

/**
 * Operator bootstrap for the account selector.  Until the UI needs multiple
 * accounts, callers deliberately select the first deterministic account; the
 * API scope is still always the UUID returned by this endpoint.
 */
export function useExchangeAccounts() {
  return useQuery({
    queryKey: exchangeAccountKeys.list(),
    queryFn: () => apiClient.get<ExchangeAccount[]>("/exchange-accounts"),
    staleTime: 60_000,
  });
}

export function useSelectedExchangeAccountId() {
  const query = useExchangeAccounts();
  return {
    ...query,
    exchangeAccountId: query.data?.[0]?.exchangeAccountId,
  };
}
