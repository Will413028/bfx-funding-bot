import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import type { Position } from "@/types";
import { usePositions } from "../use-positions";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn() },
  accountScopedPath: (id: string, path: string) =>
    `/exchange-accounts/${id}${path}`,
}));

const POSITIONS: Position[] = [
  {
    symbol: "fUST",
    reserved: "100",
    realized: "1.5",
    nCredits: 2,
    lastUpdatedMs: 1_790_000_000_000,
    lastReconciledAtMs: 1_790_000_000_000,
    lastEventSeq: 42,
  },
];

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("usePositions", () => {
  it("fetches the explicit account positions path", async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(POSITIONS);

    const { result } = renderHook(
      () => usePositions("550e8400-e29b-41d4-a716-446655440000"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith(
      "/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/positions",
    );
    expect(result.current.data).toEqual(POSITIONS);
  });
});
