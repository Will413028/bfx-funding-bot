import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import {
  executionKeys,
  offerKeys,
  positionKeys,
  uncertaintyKeys,
} from "@/lib/query-keys";
import type { Uncertainty } from "@/types";

export type UncertaintyResolutionAction =
  | "bind-to-venue"
  | "mark-not-accepted"
  | "manual-resolution";

export interface ResolveUncertaintyInput {
  uncertaintyId: string;
  action: UncertaintyResolutionAction;
  reconcileEventSeq: number;
  venueOfferId?: string;
  operatorUuid?: string;
  reason?: string;
  evidence?: Record<string, unknown>;
}

/** Build an action URL without ever exposing a venue-submit retry endpoint. */
export function uncertaintyResolutionPath(
  exchangeAccountId: string,
  uncertaintyId: string,
  action: UncertaintyResolutionAction,
): string {
  return accountScopedPath(
    exchangeAccountId,
    `/uncertainties/${encodeURIComponent(uncertaintyId)}/${action}`,
  );
}

export function useUncertainties(
  exchangeAccountId?: string,
  state: "open" | "resolved" = "open",
) {
  return useQuery({
    queryKey: uncertaintyKeys.list(exchangeAccountId, state),
    queryFn: () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.get<Uncertainty[]>(
        accountScopedPath(exchangeAccountId, "/uncertainties"),
        { params: { state } },
      );
    },
    enabled: Boolean(exchangeAccountId),
    staleTime: 15_000,
    // An unavailable uncertainty projection must remain visible to the
    // operator as an error; silently retrying does not unblock trading.
    retry: false,
  });
}

/**
 * Append one operator resolution event and refresh every affected projection.
 * The mutation intentionally has no retry: this endpoint is an audited
 * financial-state write, and repeating it could duplicate an operator action.
 */
export function useResolveUncertainty(exchangeAccountId?: string) {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (input: ResolveUncertaintyInput) => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      const body = {
        reconcileEventSeq: input.reconcileEventSeq,
        ...(input.venueOfferId !== undefined
          ? { venueOfferId: input.venueOfferId }
          : {}),
        ...(input.operatorUuid !== undefined
          ? { operatorUuid: input.operatorUuid }
          : {}),
        ...(input.reason !== undefined ? { reason: input.reason } : {}),
        evidence: input.evidence ?? {},
      };
      return apiClient.post<Uncertainty>(
        uncertaintyResolutionPath(
          exchangeAccountId,
          input.uncertaintyId,
          input.action,
        ),
        body,
      );
    },
    retry: false,
    onSuccess: async (_data, input) => {
      // Prefix invalidation also refreshes another state filter (e.g. a
      // resolved-history panel) without allowing a stale open banner to stay
      // visible after the append commits.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: uncertaintyKeys.all }),
        queryClient.invalidateQueries({ queryKey: positionKeys.all }),
        queryClient.invalidateQueries({ queryKey: offerKeys.all }),
        queryClient.invalidateQueries({ queryKey: executionKeys.all }),
      ]);
      void input;
    },
  });
}
