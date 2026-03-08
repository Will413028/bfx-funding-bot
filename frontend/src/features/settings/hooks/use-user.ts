import { useMutation, useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import type { User } from "@/types";

export function useUser() {
  return useQuery({
    queryKey: ["user", "me"],
    queryFn: () => apiClient.get<User>("/me"),
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (data: { currentPassword: string; newPassword: string }) =>
      apiClient.put<void>("/me/password", data),
  });
}
