import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({ headers: async () => new Headers() }));
const authMocks = vi.hoisted(() => ({
  getOperatorMfaSessionAccess: vi.fn(),
}));
vi.mock("@/lib/auth", () => ({
  auth: { api: { getToken: vi.fn(async () => ({ token: "jwt-123" })) } },
  getOperatorMfaSessionAccess: authMocks.getOperatorMfaSessionAccess,
}));

import { createApiKeyAction } from "@/app/[locale]/(dashboard)/api-keys/actions";
import { auth } from "@/lib/auth";

const getTokenMock = vi.mocked(auth.api.getToken);
const getOperatorMfaSessionAccess = authMocks.getOperatorMfaSessionAccess;

beforeEach(() => {
  vi.stubEnv("BFX_OPERATOR_USER_ID", "operator-1");
  getTokenMock.mockResolvedValue({ token: "jwt-123" });
  getOperatorMfaSessionAccess.mockResolvedValue({ allowed: true });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  getTokenMock.mockReset();
  getOperatorMfaSessionAccess.mockReset();
});

describe("createApiKeyAction", () => {
  it("does not send the secret when the session lacks the MFA marker", async () => {
    getOperatorMfaSessionAccess.mockResolvedValue({
      allowed: false,
      error: "mfa_required",
    });
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createApiKeyAction({
        exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
        label: "x",
        apiKey: "P",
        apiSecret: "SEC",
      }),
    ).rejects.toThrow("authTokenUnavailable");
    expect(getOperatorMfaSessionAccess).toHaveBeenCalledOnce();
    expect(getTokenMock).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts plaintext server-side with bearer and returns data", async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () =>
        new Response(
          JSON.stringify({
            data: {
              id: "1",
              exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
              label: "main",
              apiKey: "PUB",
              apiSecret: "****",
              status: "unverified",
              createdAt: "x",
            },
          }),
          { status: 201 },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await createApiKeyAction({
      exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
      label: "main",
      apiKey: "PUB",
      apiSecret: "SEC",
    });

    expect(result.apiKey).toBe("PUB");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(
      "/api/v1/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/credentials",
    );
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toContain("SEC"); // plaintext only in server fetch body
    // biome-ignore lint/suspicious/noExplicitAny: test-only header access
    expect((init as any).headers.Authorization).toBe("Bearer jwt-123");
  });

  it("throws without sending the secret when getToken rejects", async () => {
    getTokenMock.mockRejectedValue(new Error("session store down"));
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createApiKeyAction({
        exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
        label: "x",
        apiKey: "P",
        apiSecret: "SEC",
      }),
    ).rejects.toThrow("authTokenUnavailable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("throws without sending the secret when getToken returns no token", async () => {
    getTokenMock.mockResolvedValue(null as unknown as { token: string });
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createApiKeyAction({
        exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
        label: "x",
        apiKey: "P",
        apiSecret: "SEC",
      }),
    ).rejects.toThrow("authTokenUnavailable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("throws on non-ok response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "key_already_exists" }), {
            status: 409,
          }),
      ),
    );
    await expect(
      createApiKeyAction({
        exchangeAccountId: "550e8400-e29b-41d4-a716-446655440000",
        label: "x",
        apiKey: "P",
        apiSecret: "S",
      }),
    ).rejects.toThrow("key_already_exists");
  });
});
