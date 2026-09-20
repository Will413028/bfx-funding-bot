import { beforeEach, describe, expect, it, vi } from "vitest";

const dependencies = vi.hoisted(() => ({
  headers: vi.fn(),
  redirect: vi.fn(),
  signOut: vi.fn(),
}));

vi.mock("next/headers", () => ({ headers: dependencies.headers }));
vi.mock("next/navigation", () => ({ redirect: dependencies.redirect }));
vi.mock("@/lib/auth", () => ({
  auth: { api: { signOut: dependencies.signOut } },
}));

import { logoutForEnrollmentRecovery } from "./actions";

describe("enrollment recovery logout action", () => {
  beforeEach(() => {
    dependencies.headers.mockReset();
    dependencies.redirect.mockReset();
    dependencies.signOut.mockReset();
    dependencies.headers.mockResolvedValue(
      new Headers({ cookie: "better-auth.session_token=fixture-session" }),
    );
  });

  it("returns a sanitized login path after response-propagating sign-out succeeds", async () => {
    dependencies.signOut.mockResolvedValue(
      Response.json({ success: true }, { status: 200 }),
    );

    await expect(logoutForEnrollmentRecovery("zh-TW")).resolves.toEqual({
      success: true,
      redirectTo: "/zh-TW/login",
    });

    expect(dependencies.signOut).toHaveBeenCalledWith({
      headers: expect.any(Headers),
      asResponse: true,
    });
    expect(dependencies.redirect).not.toHaveBeenCalled();
  });

  it.each([
    ["endpoint rejection", () => Promise.reject(new Error("fixture-rejected"))],
    [
      "non-success response",
      () => Promise.resolve(Response.json({ success: false }, { status: 503 })),
    ],
  ])("returns failure after %s", async (_name, signOutResult) => {
    dependencies.signOut.mockImplementation(signOutResult);

    await expect(
      logoutForEnrollmentRecovery("invalid-locale"),
    ).resolves.toEqual({ success: false });
    expect(dependencies.redirect).not.toHaveBeenCalled();
  });
});
