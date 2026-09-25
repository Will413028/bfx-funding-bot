import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import {
  executionKeys,
  offerKeys,
  positionKeys,
  uncertaintyKeys,
} from "@/lib/query-keys";
import type { Uncertainty, UncertaintyResolutionRequest } from "@/types";

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

/**
 * How often the list is re-read while an adjudication waits for the daemon.
 * One list-level poll, however many panels are open: the read budget is shared.
 */
export const PENDING_RESOLUTION_POLL_MS = 5_000;

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

function pendingRequestIds(rows: Uncertainty[] | undefined): Set<string> {
  return new Set(
    (rows ?? [])
      .map((row) => row.resolutionRequest)
      .filter((request) => request?.state === "requested")
      .map((request) => request?.requestId as string),
  );
}

/**
 * Keep polling while any row waits for the daemon. Driven by the last good
 * data, so a failed read does not stop the poll and strand the panel.
 */
export function pendingResolutionPollInterval(
  rows: Uncertainty[] | undefined,
): number | false {
  return pendingRequestIds(rows).size > 0 ? PENDING_RESOLUTION_POLL_MS : false;
}

export function useUncertainties(
  exchangeAccountId?: string,
  state: "open" | "resolved" = "open",
) {
  const queryClient = useQueryClient();
  const queryKey = uncertaintyKeys.list(exchangeAccountId, state);
  return useQuery({
    queryKey,
    queryFn: async () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      const before = pendingRequestIds(
        queryClient.getQueryData<Uncertainty[]>(queryKey),
      );
      const rows = await apiClient.get<Uncertainty[]>(
        accountScopedPath(exchangeAccountId, "/uncertainties"),
        { params: { state } },
      );
      // A request that was waiting and no longer is has been settled by the
      // daemon: an applied one moved positions, offers and executions. This
      // runs once per fetch, whatever panels happen to be mounted.
      const after = pendingRequestIds(rows);
      if ([...before].some((requestId) => !after.has(requestId))) {
        void Promise.all([
          queryClient.invalidateQueries({ queryKey: positionKeys.all }),
          queryClient.invalidateQueries({ queryKey: offerKeys.all }),
          queryClient.invalidateQueries({ queryKey: executionKeys.all }),
          queryClient.invalidateQueries({
            queryKey: uncertaintyKeys.list(
              exchangeAccountId,
              state === "open" ? "resolved" : "open",
            ),
          }),
        ]);
      }
      return rows;
    },
    enabled: Boolean(exchangeAccountId),
    staleTime: 15_000,
    // An unavailable uncertainty projection must remain visible to the
    // operator as an error; silently retrying does not unblock trading.
    retry: false,
    refetchInterval: (query) => pendingResolutionPollInterval(query.state.data),
  });
}

/**
 * Queue one operator adjudication. The web API only records the request; the
 * account daemon applies it, so success here means "accepted", not "resolved".
 * The mutation stays pending until the list is re-read, so the row already
 * shows the queued request when the controls come back. No retry: a repeated
 * identical request returns the same queued row, but a network retry is still
 * not the operator's intent.
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
      return apiClient.post<UncertaintyResolutionRequest>(
        uncertaintyResolutionPath(
          exchangeAccountId,
          input.uncertaintyId,
          input.action,
        ),
        body,
      );
    },
    retry: false,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: uncertaintyKeys.all });
    },
  });
}
