import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import type { Uncertainty, UncertaintyResolutionRequest } from "@/types";
import {
  PENDING_RESOLUTION_POLL_MS,
  pendingResolutionPollInterval,
  useResolveUncertainty,
  useUncertainties,
} from "../use-uncertainties";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
  accountScopedPath: (id: string, path: string) =>
    `/exchange-accounts/${id}${path}`,
}));

const ACCOUNT_ID = "550e8400-e29b-41d4-a716-446655440000";

function setup() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: 3 },
    },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

describe("useResolveUncertainty", () => {
  beforeEach(() => vi.clearAllMocks());

  it("sends the audited resolution once without inventing client evidence", async () => {
    const { wrapper } = setup();
    vi.mocked(apiClient.post).mockResolvedValueOnce({
      requestId: "request-1",
      state: "requested",
    });
    const { result } = renderHook(() => useResolveUncertainty(ACCOUNT_ID), {
      wrapper,
    });

    await act(() =>
      result.current.mutateAsync({
        uncertaintyId: "uncertainty-1",
        action: "bind-to-venue",
        reconcileEventSeq: 42,
        venueOfferId: "venue-42",
        operatorUuid: "operator-1",
      }),
    );

    expect(apiClient.post).toHaveBeenCalledTimes(1);
    expect(apiClient.post).toHaveBeenCalledWith(
      `/exchange-accounts/${ACCOUNT_ID}/uncertainties/uncertainty-1/bind-to-venue`,
      {
        reconcileEventSeq: 42,
        venueOfferId: "venue-42",
        operatorUuid: "operator-1",
        evidence: {},
      },
    );
  });

  it("does not retry a rejected financial-state write", async () => {
    const { wrapper } = setup();
    vi.mocked(apiClient.post).mockRejectedValue(new Error("conflict"));
    const { result } = renderHook(() => useResolveUncertainty(ACCOUNT_ID), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        uncertaintyId: "uncertainty-1",
        action: "mark-not-accepted",
        reconcileEventSeq: 42,
        operatorUuid: "operator-1",
      });
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });
});

function row(
  requestState: UncertaintyResolutionRequest["state"] | null,
  uncertaintyId = "u-1",
): Uncertainty {
  return {
    uncertaintyId,
    kind: "submit_outcome_unknown",
    symbol: "fUST",
    intendedAmount: "100",
    state: "open",
    openedEventSeq: 7,
    reconcileEventSeq: null,
    resolvedEventSeq: null,
    evidenceSummary: {},
    blockedScope: {
      exchangeAccountId: ACCOUNT_ID,
      environment: "ci",
      symbol: "fUST",
    },
    resolutionContext: null,
    resolutionRequest:
      requestState === null
        ? null
        : {
            requestId: `req-${uncertaintyId}`,
            uncertaintyId,
            action: "mark_not_accepted",
            state: requestState,
            reconcileEventSeq: 12,
            createdAtMs: 1,
            processedAtMs: null,
            resolvedEventSeq: null,
            outcomeReason: null,
          },
  };
}

const PROJECTION_KEYS = [["positions"], ["offers"], ["executions"]];

function projectionRefreshes(invalidate: { mock: { calls: unknown[][] } }) {
  return invalidate.mock.calls.filter((call) =>
    PROJECTION_KEYS.some(
      (key) =>
        JSON.stringify((call[0] as { queryKey?: unknown }).queryKey) ===
        JSON.stringify(key),
    ),
  ).length;
}

describe("pendingResolutionPollInterval", () => {
  it("polls only while some row waits for the daemon", () => {
    expect(pendingResolutionPollInterval(undefined)).toBe(false);
    expect(pendingResolutionPollInterval([row(null), row("rejected")])).toBe(
      false,
    );
    expect(pendingResolutionPollInterval([row(null), row("requested")])).toBe(
      PENDING_RESOLUTION_POLL_MS,
    );
  });
});

describe("useUncertainties", () => {
  // Reset, not clear: queued once-values must not leak between tests.
  beforeEach(() => vi.resetAllMocks());

  it("refreshes projections exactly once when a waiting request settles", async () => {
    const { client, wrapper } = setup();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce([row("requested")])
      .mockResolvedValueOnce([row("requested")])
      .mockResolvedValueOnce([]) // applied: the row left the open list
      .mockResolvedValueOnce([]);
    const { result } = renderHook(() => useUncertainties(ACCOUNT_ID), {
      wrapper,
    });

    await waitFor(() => expect(result.current.data).toHaveLength(1));
    await act(() => result.current.refetch());
    expect(projectionRefreshes(invalidate)).toBe(0);
    await act(() => result.current.refetch());
    expect(projectionRefreshes(invalidate)).toBe(PROJECTION_KEYS.length);
    await act(() => result.current.refetch());
    expect(projectionRefreshes(invalidate)).toBe(PROJECTION_KEYS.length);
    expect(apiClient.get).toHaveBeenCalledTimes(4);
  });

  it("keeps polling on the last good data after a failed read", async () => {
    const { client, wrapper } = setup();
    vi.mocked(apiClient.get).mockResolvedValueOnce([row("requested")]);
    const { result } = renderHook(() => useUncertainties(ACCOUNT_ID), {
      wrapper,
    });
    await waitFor(() => expect(result.current.data).toHaveLength(1));

    const query = client
      .getQueryCache()
      .find({ queryKey: ["uncertainties", ACCOUNT_ID, "list", "open"] });
    if (!query) throw new Error("list query missing");
    // A failed read (429, 503, network) keeps the previous data.
    query.setState({ status: "error", error: new Error("429") });
    const interval = query.observers[0]?.options.refetchInterval;
    expect(typeof interval).toBe("function");
    expect((interval as (q: typeof query) => number | false)(query)).toBe(
      PENDING_RESOLUTION_POLL_MS,
    );
  });
});
