import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, accountScopedPath, apiClient } from "@/lib/api-client";
import { configKeys } from "@/lib/query-keys";
import type { StrategyConfig, UserConfig } from "@/types";

export function useConfig(exchangeAccountId?: string) {
  return useQuery({
    queryKey: configKeys.current(exchangeAccountId),
    queryFn: async () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      try {
        return await apiClient.get<UserConfig>(
          accountScopedPath(exchangeAccountId, "/config-draft"),
        );
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          return null;
        }
        throw err;
      }
    },
    enabled: Boolean(exchangeAccountId),
  });
}

export function useSaveConfig(exchangeAccountId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (config: StrategyConfig) => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.put<UserConfig>(
        accountScopedPath(exchangeAccountId, "/config-draft"),
        config,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: configKeys.all });
    },
  });
}

export function useResetConfig(exchangeAccountId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.del<void>(
        accountScopedPath(exchangeAccountId, "/config-draft"),
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: configKeys.all });
    },
  });
}
