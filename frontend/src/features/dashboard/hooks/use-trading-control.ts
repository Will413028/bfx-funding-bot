import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { accountScopedPath, apiClient } from "@/lib/api-client";
import { fundingStatusKeys, tradingControlKeys } from "@/lib/query-keys";
import type {
  TradingControlAction,
  TradingControlOverview,
  TradingControlRequest,
} from "@/types";

/** Re-read while a request waits for the daemon; one poll for the whole panel. */
export const PENDING_CONTROL_POLL_MS = 3_000;
/** Otherwise the state still changes on its own (automatic stops). */
export const IDLE_CONTROL_POLL_MS = 30_000;

export function tradingControlPath(
  exchangeAccountId: string,
  suffix: "" | `/${string}` = "",
): string {
  return accountScopedPath(
    exchangeAccountId,
    `/trading-control${suffix}` as `/${string}`,
  );
}

function pendingRequestIds(
  overview: TradingControlOverview | undefined,
): Set<string> {
  return new Set(
    (overview?.requests ?? [])
      .filter((request) => request.state === "requested")
      .map((request) => request.request_id),
  );
}

/**
 * Poll fast while any request waits. Driven by the last good data, so a failed
 * read neither stops the poll nor strands the panel on a stale "waiting".
 */
export function tradingControlPollInterval(
  overview: TradingControlOverview | undefined,
): number {
  return pendingRequestIds(overview).size > 0
    ? PENDING_CONTROL_POLL_MS
    : IDLE_CONTROL_POLL_MS;
}

export function useTradingControl(exchangeAccountId?: string) {
  const queryClient = useQueryClient();
  const queryKey = tradingControlKeys.overview(exchangeAccountId);
  return useQuery({
    queryKey,
    queryFn: async () => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      const before = pendingRequestIds(
        queryClient.getQueryData<TradingControlOverview>(queryKey),
      );
      const overview = await apiClient.get<TradingControlOverview>(
        tradingControlPath(exchangeAccountId),
      );
      // A request that was waiting and no longer is was settled by the
      // daemon: the trading state (and so the funding status) moved.
      const after = pendingRequestIds(overview);
      if ([...before].some((requestId) => !after.has(requestId))) {
        void queryClient.invalidateQueries({
          queryKey: fundingStatusKeys.detail(exchangeAccountId),
        });
      }
      return overview;
    },
    enabled: Boolean(exchangeAccountId),
    // An unreadable trading state must stay visible as an error; the poll
    // keeps trying on its own schedule.
    retry: false,
    refetchInterval: (query) => tradingControlPollInterval(query.state.data),
  });
}

export interface TradingControlInput {
  action: TradingControlAction;
  reason: string;
}

/**
 * Queue one operator request. The web API only records it; the account daemon
 * applies it, so success here means "accepted", not "done". No retry: a network
 * retry is not the operator's intent.
 */
export function useTradingControlRequest(exchangeAccountId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: TradingControlInput) => {
      if (!exchangeAccountId) throw new Error("exchangeAccountNotSelected");
      return apiClient.post<
        Pick<TradingControlRequest, "request_id" | "action" | "state">
      >(tradingControlPath(exchangeAccountId, `/${input.action}`), {
        reason: input.reason,
      });
    },
    retry: false,
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: tradingControlKeys.overview(exchangeAccountId),
      });
    },
  });
}
