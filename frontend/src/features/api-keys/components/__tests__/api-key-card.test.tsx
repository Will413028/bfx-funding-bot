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
    status: "verified",
    createdAt: "2026-01-01",
    verifiedAt: null,
    lastVerifyError: null,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("ApiKeyCard", () => {
  it("labels a newly created credential as pending verification", () => {
    render(<ApiKeyCard apiKey={makeKey({ status: "pending" })} />);

    expect(screen.getByText("Pending verification").textContent).toContain(
      "Pending verification",
    );
  });

  it.each(["pending", "verified", "failed", "revoked", "retired"] as const)(
    "is read-only: no verify or delete action (%s)",
    (status) => {
      render(<ApiKeyCard apiKey={makeKey({ status })} />);

      expect(screen.queryAllByRole("button")).toHaveLength(0);
    },
  );

  it("surfaces lastVerifyError reason when status is failed", () => {
    render(
      <ApiKeyCard
        apiKey={makeKey({
          status: "failed",
          lastVerifyError: "withdraw permission is enabled",
        })}
      />,
    );

    const reason = screen.getByTestId("verify-error-reason");
    expect(reason.textContent).toContain("withdraw permission is enabled");
  });

  it("surfaces a permission error while a credential remains pending", () => {
    render(
      <ApiKeyCard
        apiKey={makeKey({
          status: "pending",
          lastVerifyError: "funding write permission required",
        })}
      />,
    );

    expect(screen.getByTestId("verify-error-reason").textContent).toContain(
      "funding write permission required",
    );
  });

  it("does not render a reason when failed but no lastVerifyError", () => {
    render(
      <ApiKeyCard
        apiKey={makeKey({ status: "failed", lastVerifyError: null })}
      />,
    );

    expect(screen.queryByTestId("verify-error-reason")).toBeNull();
  });

  it("does not render a reason for a verified key even if a stale reason exists", () => {
    render(
      <ApiKeyCard
        apiKey={makeKey({
          status: "verified",
          lastVerifyError: "old failure",
        })}
      />,
    );

    expect(screen.queryByText("old failure")).toBeNull();
  });
});
