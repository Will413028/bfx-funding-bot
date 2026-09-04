import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import type { ExchangeAccount } from "@/types";
import { useSelectedExchangeAccountId } from "../use-exchange-accounts";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn() },
}));

const ACCOUNTS: ExchangeAccount[] = [
  {
    exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
    venue: "bitfinex",
    label: "Primary",
    lifecycleStatus: "active",
    role: "owner",
  },
];

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useSelectedExchangeAccountId", () => {
  it("bootstraps and returns the server-provided UUID", async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(ACCOUNTS);

    const { result } = renderHook(() => useSelectedExchangeAccountId(), {
      wrapper,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(apiClient.get).toHaveBeenCalledWith("/exchange-accounts");
    expect(result.current.exchangeAccountId).toBe(
      ACCOUNTS[0].exchangeAccountId,
    );
  });
});
