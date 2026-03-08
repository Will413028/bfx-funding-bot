import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiClient } from "@/lib/api-client";
import { configKeys } from "@/lib/query-keys";
import type { StrategyConfig, UserConfig } from "@/types";

export function useConfig() {
  return useQuery({
    queryKey: configKeys.current(),
    queryFn: async () => {
      try {
        return await apiClient.get<UserConfig>("/configs");
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          return null;
        }
        throw err;
      }
    },
  });
}

export function useSaveConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (config: StrategyConfig) =>
      apiClient.put<UserConfig>("/configs", config),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: configKeys.all });
    },
  });
}

export function useResetConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.del<void>("/configs"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: configKeys.all });
    },
  });
}
