import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import type { ExecutionEvent } from "@/types";
import {
  EXECUTION_EVENTS_PAGE_SIZE,
  getNextEventsPageParam,
  useExecutionEvents,
} from "../use-execution-events";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn() },
}));

function makeEvent(eventSeq: number): ExecutionEvent {
  return {
    eventSeq,
    eventType: "ORDER_FILL",
    occurredAtMs: 1_790_000_000_000,
    symbol: "fUST",
    venueOfferId: null,
    cid: null,
    amount: "10",
    rate: 0.0002,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("getNextEventsPageParam", () => {
  it("returns the smallest event_seq of a full page as the before cursor", () => {
    const fullPage = Array.from(
      { length: EXECUTION_EVENTS_PAGE_SIZE },
      (_, i) => makeEvent(200 - i),
    );
    expect(getNextEventsPageParam(fullPage)).toBe(
      200 - (EXECUTION_EVENTS_PAGE_SIZE - 1),
    );
  });

  it("returns undefined for a short page (log exhausted)", () => {
    expect(getNextEventsPageParam([makeEvent(3)])).toBeUndefined();
    expect(getNextEventsPageParam([])).toBeUndefined();
  });
});

describe("useExecutionEvents", () => {
  it("fetches the first page without a before cursor, then pages with it", async () => {
    const firstPage = Array.from(
      { length: EXECUTION_EVENTS_PAGE_SIZE },
      (_, i) => makeEvent(200 - i),
    );
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce(firstPage)
      .mockResolvedValueOnce([makeEvent(100)]);

    const { result } = renderHook(() => useExecutionEvents(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith("/executions", {
      params: { limit: String(EXECUTION_EVENTS_PAGE_SIZE) },
    });
    expect(result.current.hasNextPage).toBe(true);

    result.current.fetchNextPage();
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));
    expect(apiClient.get).toHaveBeenLastCalledWith("/executions", {
      params: {
        limit: String(EXECUTION_EVENTS_PAGE_SIZE),
        before: String(200 - (EXECUTION_EVENTS_PAGE_SIZE - 1)),
      },
    });
    // Short second page → no further cursor.
    expect(result.current.hasNextPage).toBe(false);
  });
});
