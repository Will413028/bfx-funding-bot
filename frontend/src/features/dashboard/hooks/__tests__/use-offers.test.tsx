import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import type { OfferClaim } from "@/types";
import { useOffers } from "../use-offers";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn() },
  accountScopedPath: (id: string, path: string) =>
    `/exchange-accounts/${id}${path}`,
}));

const OFFERS: OfferClaim[] = [
  {
    offerKey: "7b1f0c9e-1111-4222-8333-444455556666",
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

    const { result } = renderHook(
      () => useOffers("550e8400-e29b-41d4-a716-446655440000"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith(
      "/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/offers",
      { params: undefined },
    );
    expect(result.current.data).toEqual(OFFERS);
  });

  it("passes an explicit state filter through as a query param", async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce([]);

    const { result } = renderHook(
      () => useOffers("550e8400-e29b-41d4-a716-446655440000", "released"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith(
      "/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/offers",
      { params: { state: "released" } },
    );
  });
});
