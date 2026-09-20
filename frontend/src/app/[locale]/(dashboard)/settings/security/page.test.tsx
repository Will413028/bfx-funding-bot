import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@/lib/test-utils";

const requestHeaders = new Headers({ cookie: "fixture-session-cookie" });
const getOperatorEnrollmentState = vi.hoisted(() => vi.fn());
const notFound = vi.hoisted(() => vi.fn());

vi.mock("next/headers", () => ({
  headers: vi.fn(async () => requestHeaders),
}));
vi.mock("next/navigation", () => ({ notFound }));
vi.mock("@/lib/operator-enrollment", () => ({ getOperatorEnrollmentState }));
vi.mock("@/features/settings/components/two-factor-enrollment", () => ({
  TwoFactorEnrollment: ({ enrolled }: { enrolled: boolean }) => (
    <p>{enrolled ? "fixture-enrolled" : "fixture-not-enrolled"}</p>
  ),
}));

import SecurityPage from "./page";

describe("SecurityPage", () => {
  const previousOperatorId = process.env.BFX_OPERATOR_USER_ID;

  afterEach(() => {
    cleanup();
    process.env.BFX_OPERATOR_USER_ID = previousOperatorId;
  });

  beforeEach(() => {
    process.env.BFX_OPERATOR_USER_ID = "operator-1";
    getOperatorEnrollmentState.mockReset();
    notFound.mockReset().mockImplementation(() => {
      throw new Error("NEXT_NOT_FOUND");
    });
  });

  it("passes only boolean enrollment state through the server guard", async () => {
    getOperatorEnrollmentState.mockResolvedValue({
      allowed: true,
      enrolled: false,
    });

    render(await SecurityPage());

    expect(screen.getByText("fixture-not-enrolled")).toBeDefined();
    expect(getOperatorEnrollmentState).toHaveBeenCalledWith(
      requestHeaders,
      "operator-1",
    );
  });

  it("fails closed when server-side identity access is denied", async () => {
    getOperatorEnrollmentState.mockResolvedValue({ allowed: false });

    await expect(SecurityPage()).rejects.toThrow("NEXT_NOT_FOUND");
    expect(notFound).toHaveBeenCalledOnce();
  });
});
