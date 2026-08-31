import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import type { ExecutionEvent, ExecutionEventsResponse } from "@/types";
import {
  EXECUTION_EVENTS_PAGE_SIZE,
  getNextEventsPageParam,
  useExecutionEvents,
} from "../use-execution-events";

vi.mock("@/lib/api-client", () => ({
  apiClient: { getList: vi.fn() },
  accountScopedPath: (id: string, path: string) =>
    `/exchange-accounts/${id}${path}`,
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

function makePage(
  eventSeqs: number[],
  pagination: ExecutionEventsResponse["pagination"],
): ExecutionEventsResponse {
  return { data: eventSeqs.map(makeEvent), pagination };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("getNextEventsPageParam", () => {
  it("returns nextBefore when the server says there is more", () => {
    const page = makePage([200, 199], { hasMore: true, nextBefore: 199 });
    expect(getNextEventsPageParam(page)).toBe(199);
  });

  it("returns undefined when the log is exhausted", () => {
    expect(
      getNextEventsPageParam(
        makePage([3], { hasMore: false, nextBefore: null }),
      ),
    ).toBeUndefined();
    expect(
      getNextEventsPageParam(
        makePage([], { hasMore: false, nextBefore: null }),
      ),
    ).toBeUndefined();
  });
});

describe("useExecutionEvents", () => {
  it("fetches the first page without a cursor, then pages with nextBefore", async () => {
    vi.mocked(apiClient.getList)
      .mockResolvedValueOnce(
        makePage([200, 199], { hasMore: true, nextBefore: 199 }),
      )
      .mockResolvedValueOnce(
        makePage([100], { hasMore: false, nextBefore: null }),
      );

    const { result } = renderHook(
      () =>
        useExecutionEvents({
          exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
        }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.getList).toHaveBeenCalledWith(
      "/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/executions",
      { params: { limit: String(EXECUTION_EVENTS_PAGE_SIZE) } },
    );
    expect(result.current.hasNextPage).toBe(true);

    result.current.fetchNextPage();
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));
    expect(apiClient.getList).toHaveBeenLastCalledWith(
      "/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/executions",
      {
        params: {
          limit: String(EXECUTION_EVENTS_PAGE_SIZE),
          before: "199",
        },
      },
    );
    // Envelope says exhausted → no further cursor.
    expect(result.current.hasNextPage).toBe(false);
  });

  it("passes the event_type filter and custom page size through", async () => {
    vi.mocked(apiClient.getList).mockResolvedValueOnce(
      makePage([50], { hasMore: false, nextBefore: null }),
    );

    const { result } = renderHook(
      () =>
        useExecutionEvents({
          exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
          eventType: "ORDER_FILL",
          pageSize: 50,
        }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.getList).toHaveBeenCalledWith(
      "/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/executions",
      { params: { limit: "50", event_type: "ORDER_FILL" } },
    );
  });
});
