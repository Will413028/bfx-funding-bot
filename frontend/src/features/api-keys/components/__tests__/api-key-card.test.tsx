import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";
import type { ApiKey } from "@/types";
import { ApiKeyCard } from "../api-key-card";

function makeKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: "1",
    exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
    label: "main",
    apiKey: "PUBKEY12345",
    apiSecret: "****",
    status: "active",
    createdAt: "2026-01-01",
    verifiedAt: "2026-01-01",
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("ApiKeyCard", () => {
  it.each([
    ["active", "Active"],
    ["retired", "Retired"],
  ] as const)("labels a %s credential", (status, label) => {
    render(<ApiKeyCard apiKey={makeKey({ status })} />);

    expect(screen.getByText(label)).toBeDefined();
  });

  it.each(["active", "retired"] as const)(
    "is read-only: no verify or delete action (%s)",
    (status) => {
      render(<ApiKeyCard apiKey={makeKey({ status })} />);

      expect(screen.queryAllByRole("button")).toHaveLength(0);
    },
  );
});
