import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api-client";
import { userKeys } from "@/lib/query-keys";
import type { User } from "@/types";

export function useUser() {
  return useQuery({
    queryKey: userKeys.me(),
    queryFn: () => apiClient.get<User>("/me"),
  });
}
