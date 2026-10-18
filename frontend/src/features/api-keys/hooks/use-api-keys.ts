import { useQuery } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { apiKeyKeys } from "@/lib/query-keys";
import type { ApiKey } from "@/types";

/**
 * The account's credentials, read-only: the web API no longer creates,
 * verifies or deletes them (operator CLI bootstrap and `bfx-op rotate-credential`).
 */
export function useApiKeys(exchangeAccountId?: string) {
  return useQuery({
    queryKey: apiKeyKeys.list(exchangeAccountId),
    queryFn: () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.get<ApiKey[]>(
        accountScopedPath(exchangeAccountId, "/credentials"),
      );
    },
    enabled: Boolean(exchangeAccountId),
  });
}
