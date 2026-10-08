import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import { fundingStatusKeys, tradingControlKeys } from "@/lib/query-keys";
import type { TradingControlOverview, TradingControlRequest } from "@/types";
import {
  IDLE_CONTROL_POLL_MS,
  PENDING_CONTROL_POLL_MS,
  tradingControlPollInterval,
  useCurrencyToggleRequest,
  useTradingControl,
  useTradingControlRequest,
} from "../use-trading-control";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
  accountScopedPath: (id: string, path: string) =>
    `/exchange-accounts/${id}${path}`,
}));

const ACCOUNT_ID = "550e8400-e29b-41d4-a716-446655440000";

function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 3 } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

function overview(requests: Partial<TradingControlRequest>[] = []) {
  return {
    trading_state: null,
    cancel_all: [],
    running: { backend_digest: null, source_revision: null },
    latest_deployment: null,
    currencies: [],
    requests: requests.map((request, index) => ({
      request_id: `r-${index}`,
      action: "resume",
      reason: "x",
      requested_by: "will",
      created_at_ms: 1,
      state: "requested",
      processed_at_ms: null,
      outcome_reason: null,
      ...request,
    })),
  } as TradingControlOverview;
}

describe("tradingControlPollInterval", () => {
  it("polls fast only while a request waits for the daemon", () => {
    expect(tradingControlPollInterval(overview([{ state: "requested" }]))).toBe(
      PENDING_CONTROL_POLL_MS,
    );
    expect(tradingControlPollInterval(overview([{ state: "applied" }]))).toBe(
      IDLE_CONTROL_POLL_MS,
    );
    // No data yet (or a failed first read): keep polling on the idle cadence.
    expect(tradingControlPollInterval(undefined)).toBe(IDLE_CONTROL_POLL_MS);
  });
});

describe("useTradingControl", () => {
  beforeEach(() => vi.clearAllMocks());

  it("refreshes the funding status once a waiting request is settled", async () => {
    const { client, wrapper } = setup();
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce(
        overview([{ request_id: "r-0", state: "requested" }]),
      )
      .mockResolvedValueOnce(
        overview([{ request_id: "r-0", state: "applied" }]),
      );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useTradingControl(ACCOUNT_ID), {
      wrapper,
    });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(apiClient.get).toHaveBeenCalledWith(
      `/exchange-accounts/${ACCOUNT_ID}/trading-control`,
    );
    expect(invalidate).not.toHaveBeenCalled();
    await act(() => result.current.refetch());
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: fundingStatusKeys.detail(ACCOUNT_ID),
    });
  });

  it("keeps the last good overview when a re-read fails", async () => {
    const { wrapper } = setup();
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce(overview([{ state: "requested" }]))
      .mockRejectedValueOnce(new Error("offline"));
    const { result } = renderHook(() => useTradingControl(ACCOUNT_ID), {
      wrapper,
    });
    await waitFor(() => expect(result.current.data).toBeDefined());
    // Read it once: TanStack re-renders only for the result fields in use.
    expect(result.current.isError).toBe(false);
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data?.requests[0].state).toBe("requested");
    // The poll keeps its pending cadence off the last good data.
    expect(tradingControlPollInterval(result.current.data)).toBe(
      PENDING_CONTROL_POLL_MS,
    );
  });
});

describe("useTradingControlRequest", () => {
  beforeEach(() => vi.clearAllMocks());

  it("sends only the reason, and never retries", async () => {
    const { client, wrapper } = setup();
    vi.mocked(apiClient.post).mockResolvedValue({
      request_id: "r",
      action: "kill",
      state: "requested",
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useTradingControlRequest(ACCOUNT_ID), {
      wrapper,
    });
    await act(() =>
      result.current.mutateAsync({ action: "kill", reason: "incident" }),
    );
    await act(() =>
      result.current.mutateAsync({ action: "resume", reason: "back" }),
    );
    expect(vi.mocked(apiClient.post).mock.calls).toEqual([
      [
        `/exchange-accounts/${ACCOUNT_ID}/trading-control/kill`,
        { reason: "incident" },
      ],
      [
        `/exchange-accounts/${ACCOUNT_ID}/trading-control/resume`,
        { reason: "back" },
      ],
    ]);
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: tradingControlKeys.overview(ACCOUNT_ID),
    });

    vi.mocked(apiClient.post).mockReset();
    vi.mocked(apiClient.post).mockRejectedValue(new Error("conflict"));
    act(() => {
      result.current.mutate({ action: "resume", reason: "x" });
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });
});

describe("currency toggles", () => {
  beforeEach(() => vi.clearAllMocks());

  it("a waiting toggle keeps the fast poll like any other request", () => {
    const waiting = {
      ...overview(),
      currencies: [
        {
          symbol: "fUST",
          requests: [{ request_id: "c-0", state: "requested" }],
        },
      ],
    } as TradingControlOverview;
    expect(tradingControlPollInterval(waiting)).toBe(PENDING_CONTROL_POLL_MS);
  });

  it("queues enable/disable for one currency and never retries", async () => {
    const { client, wrapper } = setup();
    vi.mocked(apiClient.post).mockRejectedValue(new Error("conflict"));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useCurrencyToggleRequest(ACCOUNT_ID), {
      wrapper,
    });
    act(() => {
      result.current.mutate({
        symbol: "fUST",
        action: "disable",
        reason: "maintenance",
      });
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(vi.mocked(apiClient.post).mock.calls).toEqual([
      [
        `/exchange-accounts/${ACCOUNT_ID}/trading-control/currencies/fUST/disable`,
        { reason: "maintenance" },
      ],
    ]);
    vi.mocked(apiClient.post).mockResolvedValue({ request_id: "c" });
    await act(() =>
      result.current.mutateAsync({
        symbol: "fUST",
        action: "enable",
        reason: "back",
      }),
    );
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: tradingControlKeys.overview(ACCOUNT_ID),
    });
  });
});
