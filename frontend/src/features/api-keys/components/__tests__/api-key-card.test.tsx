import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";
import type { ApiKey } from "@/types";
import { ApiKeyCard } from "../api-key-card";

function makeKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: "1",
    label: "main",
    apiKey: "PUBKEY12345",
    apiSecret: "****",
    exchangeStatus: "verified",
    createdAt: "2026-01-01",
    ...overrides,
  };
}

const noop = vi.fn();

afterEach(() => cleanup());

describe("ApiKeyCard", () => {
  it("surfaces lastVerifyError reason when status is failed", () => {
    render(
      <ApiKeyCard
        apiKey={makeKey({
          exchangeStatus: "failed",
          lastVerifyError: "withdraw permission is enabled",
        })}
        onVerify={noop}
        onDelete={noop}
        isVerifying={false}
        isDeleting={false}
      />,
    );

    const reason = screen.getByTestId("verify-error-reason");
    expect(reason.textContent).toContain("withdraw permission is enabled");
  });

  it("does not render a reason when failed but no lastVerifyError", () => {
    render(
      <ApiKeyCard
        apiKey={makeKey({ exchangeStatus: "failed", lastVerifyError: null })}
        onVerify={noop}
        onDelete={noop}
        isVerifying={false}
        isDeleting={false}
      />,
    );

    expect(screen.queryByTestId("verify-error-reason")).toBeNull();
  });

  it("does not render a reason for a verified key even if a stale reason exists", () => {
    render(
      <ApiKeyCard
        apiKey={makeKey({
          exchangeStatus: "verified",
          lastVerifyError: "old failure",
        })}
        onVerify={noop}
        onDelete={noop}
        isVerifying={false}
        isDeleting={false}
      />,
    );

    expect(screen.queryByText("old failure")).toBeNull();
  });
});
