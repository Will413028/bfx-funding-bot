import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const authHandlers = vi.hoisted(() => ({
  GET: vi.fn(async () => new Response("delegated", { status: 200 })),
  POST: vi.fn(async () => new Response("delegated", { status: 200 })),
}));

vi.mock("better-auth/next-js", () => ({
  toNextJsHandler: vi.fn(() => authHandlers),
}));

vi.mock("@/lib/auth", () => ({ auth: {} }));

import { GET, POST } from "./route";

describe("public Better Auth boundary", () => {
  beforeEach(() => {
    authHandlers.GET.mockClear();
    authHandlers.POST.mockClear();
  });

  it.each(["token", "sign-jwt", "verify-jwt"])(
    "rejects external JWT endpoint /%s",
    async (endpoint) => {
      const response = await GET(
        new NextRequest(`http://localhost/api/auth/${endpoint}`),
      );

      expect(response.status).toBe(404);
      expect(authHandlers.GET).not.toHaveBeenCalled();
    },
  );

  it("rejects encoded and trailing-slash variants of the token endpoint", async () => {
    const encoded = await GET(
      new NextRequest("http://localhost/api/auth/%74oken"),
    );
    const trailingSlash = await GET(
      new NextRequest("http://localhost/api/auth/token/"),
    );

    expect(encoded.status).toBe(404);
    expect(trailingSlash.status).toBe(404);
    expect(authHandlers.GET).not.toHaveBeenCalled();
  });

  it("keeps the session endpoint delegated without exposing a JWT header", async () => {
    const response = await GET(
      new NextRequest("http://localhost/api/auth/get-session"),
    );

    expect(response.status).toBe(200);
    expect(authHandlers.GET).toHaveBeenCalledOnce();
  });

  it("rejects POST JWT signing as well", async () => {
    const response = await POST(
      new NextRequest("http://localhost/api/auth/sign-jwt", {
        method: "POST",
        body: JSON.stringify({ payload: { sub: "operator-1" } }),
      }),
    );

    expect(response.status).toBe(404);
    expect(authHandlers.POST).not.toHaveBeenCalled();
  });
});
