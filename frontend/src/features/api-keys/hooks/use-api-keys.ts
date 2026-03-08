import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { apiKeyKeys } from "@/lib/query-keys";
import type { ApiKey, VerifyResult } from "@/types";

export function useApiKeys() {
  return useQuery({
    queryKey: apiKeyKeys.list(),
    queryFn: () => apiClient.get<ApiKey[]>("/api-keys"),
  });
}

export function useCreateApiKey() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { label: string; apiKey: string; apiSecret: string }) =>
      apiClient.post<ApiKey>("/api-keys", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: apiKeyKeys.all });
    },
  });
}

export function useDeleteApiKey() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiClient.del<void>(`/api-keys/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: apiKeyKeys.all });
    },
  });
}

export function useVerifyApiKey() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiClient.post<VerifyResult>(`/api-keys/${id}/verify`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: apiKeyKeys.all });
    },
  });
}
