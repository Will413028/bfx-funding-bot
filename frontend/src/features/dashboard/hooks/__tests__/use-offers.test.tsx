import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import type { OfferClaim } from "@/types";
import { useOffers } from "../use-offers";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn() },
}));

const OFFERS: OfferClaim[] = [
  {
    cid: 17123,
    venueOfferId: "3456789",
    state: "claimed",
    symbol: "fUST",
    sizeUsdt: "500.25",
    occurredAtMs: 1_790_000_000_000,
    lastUpdatedMs: 1_790_000_060_000,
  },
];

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useOffers", () => {
  it("fetches GET /offers without a state filter by default", async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(OFFERS);

    const { result } = renderHook(() => useOffers(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith("/offers", {
      params: undefined,
    });
    expect(result.current.data).toEqual(OFFERS);
  });

  it("passes an explicit state filter through as a query param", async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce([]);

    const { result } = renderHook(() => useOffers("released"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith("/offers", {
      params: { state: "released" },
    });
  });
});
