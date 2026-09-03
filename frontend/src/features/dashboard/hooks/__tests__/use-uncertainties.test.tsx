import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import { useResolveUncertainty } from "../use-uncertainties";

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
    vi.mocked(apiClient.post).mockResolvedValueOnce({ state: "resolved" });
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
