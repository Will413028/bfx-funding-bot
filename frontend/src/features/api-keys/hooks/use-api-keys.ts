import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createApiKeyAction } from "@/app/[locale]/(dashboard)/api-keys/actions";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { apiKeyKeys } from "@/lib/query-keys";
import type { ApiKey, VerifyResult } from "@/types";

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

export function useCreateApiKey(exchangeAccountId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      label: string;
      apiKey: string;
      apiSecret: string;
    }) => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return createApiKeyAction({ ...data, exchangeAccountId });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: apiKeyKeys.all });
    },
  });
}

export function useDeleteApiKey(exchangeAccountId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.del<void>(
        accountScopedPath(exchangeAccountId, `/credentials/${id}`),
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: apiKeyKeys.all });
    },
  });
}

export function useVerifyApiKey(exchangeAccountId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      (() => {
        if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
        return apiClient.post<VerifyResult>(
          accountScopedPath(exchangeAccountId, `/credentials/${id}/verify`),
        );
      })(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: apiKeyKeys.all });
    },
  });
}
