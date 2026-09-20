import { beforeEach, describe, expect, it, vi } from "vitest";

const getSession = vi.hoisted(() => vi.fn());

vi.mock("@/lib/auth", () => ({
  auth: { api: { getSession } },
}));

import { getOperatorEnrollmentState } from "../operator-enrollment";

const requestHeaders = new Headers({ cookie: "fixture-session-cookie" });

function sessionUser(
  overrides: Partial<{
    id: string;
    banned: boolean;
    role: string;
    twoFactorEnabled: boolean;
  }> = {},
) {
  return {
    session: { token: "fixture-session-token" },
    user: {
      id: "operator-1",
      banned: false,
      role: "user",
      twoFactorEnabled: false,
      ...overrides,
    },
  };
}

describe("getOperatorEnrollmentState", () => {
  beforeEach(() => {
    getSession.mockReset();
  });

  it("denies missing operator configuration before reading a session", async () => {
    await expect(
      getOperatorEnrollmentState(requestHeaders, undefined),
    ).resolves.toEqual({ allowed: false });
    expect(getSession).not.toHaveBeenCalled();
  });

  it("denies an anonymous session", async () => {
    getSession.mockResolvedValue(null);

    await expect(
      getOperatorEnrollmentState(requestHeaders, "operator-1"),
    ).resolves.toEqual({ allowed: false });
  });

  it("denies a non-operator identity", async () => {
    getSession.mockResolvedValue(sessionUser({ id: "another-user" }));

    await expect(
      getOperatorEnrollmentState(requestHeaders, "operator-1"),
    ).resolves.toEqual({ allowed: false });
  });

  it("denies a banned operator", async () => {
    getSession.mockResolvedValue(sessionUser({ banned: true }));

    await expect(
      getOperatorEnrollmentState(requestHeaders, "operator-1"),
    ).resolves.toEqual({ allowed: false });
  });

  it("allows the exact non-banned operator without role authority", async () => {
    getSession.mockResolvedValue(sessionUser({ role: "user" }));

    await expect(
      getOperatorEnrollmentState(requestHeaders, "operator-1"),
    ).resolves.toEqual({ allowed: true, enrolled: false });
    expect(getSession).toHaveBeenCalledWith({
      headers: requestHeaders,
      query: { disableCookieCache: true },
    });
  });

  it("returns only authoritative enrollment status for an enrolled operator", async () => {
    getSession.mockResolvedValue(sessionUser({ twoFactorEnabled: true }));

    await expect(
      getOperatorEnrollmentState(requestHeaders, "operator-1"),
    ).resolves.toEqual({ allowed: true, enrolled: true });
  });

  it("fails closed when the session lookup fails", async () => {
    getSession.mockRejectedValue(new Error("fixture session failure"));

    await expect(
      getOperatorEnrollmentState(requestHeaders, "operator-1"),
    ).resolves.toEqual({ allowed: false });
  });
});
