import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import ApiKeysPage from "@/app/[locale]/(dashboard)/api-keys/page";
import { cleanup, render, screen } from "@/lib/test-utils";
import type { ApiKey } from "@/types";

const { keys } = vi.hoisted(() => ({ keys: { data: [] as ApiKey[] } }));

vi.mock("@/features/accounts/hooks/use-exchange-accounts", () => ({
  useSelectedExchangeAccountId: () => ({
    isLoading: false,
    isError: false,
    exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
  }),
}));

vi.mock("@/features/api-keys/hooks/use-api-keys", async (importOriginal) => ({
  ...(await importOriginal<
    typeof import("@/features/api-keys/hooks/use-api-keys")
  >()),
  useApiKeys: () => ({
    data: keys.data,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ApiKeysPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  keys.data = [];
});

describe("API keys page", () => {
  it("an empty list points to the operator CLI and offers no way to add a key", () => {
    renderPage();

    expect(
      screen.getByText(
        "No API keys yet. Add one with the operator CLI bootstrap, or replace the active key with bfx-op rotate-credential.",
      ),
    ).toBeDefined();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryAllByRole("dialog")).toHaveLength(0);
  });

  it("lists credentials read-only", () => {
    keys.data = [
      {
        id: "1",
        exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
        label: "main",
        apiKey: "PUBKEY12345",
        apiSecret: "****",
        status: "verified",
        createdAt: "2026-01-01",
        verifiedAt: null,
        lastVerifyError: null,
      },
    ];
    renderPage();

    expect(screen.getByText("main")).toBeDefined();
    expect(screen.getByText("Verified")).toBeDefined();
    expect(screen.getByText(/sudo bfx-op rotate-credential/)).toBeDefined();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
