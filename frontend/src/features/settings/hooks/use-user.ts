import { useMutation, useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { userKeys } from "@/lib/query-keys";
import type { User } from "@/types";

export function useUser() {
  return useQuery({
    queryKey: userKeys.me(),
    queryFn: () => apiClient.get<User>("/me"),
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (data: { currentPassword: string; newPassword: string }) =>
      apiClient.put<void>("/me/password", data),
  });
}
