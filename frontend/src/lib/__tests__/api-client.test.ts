import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockFetch = vi.fn();

beforeEach(() => {
  vi.resetModules();
  mockFetch.mockReset();
  vi.stubGlobal("fetch", mockFetch);
  vi.stubEnv("API_URL", "http://localhost:8080");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.doUnmock("@/lib/auth");
  vi.doUnmock("next/headers");
});

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function loadApiClient() {
  return import("../api-client");
}

describe("apiClient.get", () => {
  it("does not mint or forward a server-side request without MFA marker access", async () => {
    const getToken = vi.fn();
    const getOperatorMfaSessionAccess = vi
      .fn()
      .mockResolvedValue({ allowed: false, error: "mfa_required" });
    vi.stubGlobal("window", undefined);
    vi.stubEnv("BFX_OPERATOR_USER_ID", "operator-1");
    vi.doMock("next/headers", () => ({
      headers: vi.fn(async () => new Headers()),
    }));
    vi.doMock("@/lib/auth", () => ({
      auth: { api: { getToken } },
      getOperatorMfaSessionAccess,
    }));

    const { apiClient, ApiError } = await loadApiClient();

    await expect(
      apiClient.get(
        "/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/positions",
      ),
    ).rejects.toMatchObject({
      status: 403,
      code: "mfa_required",
    } satisfies Partial<InstanceType<typeof ApiError>>);
    expect(getOperatorMfaSessionAccess).toHaveBeenCalledOnce();
    expect(getToken).not.toHaveBeenCalled();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("unwraps data envelope", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ data: { id: 1, name: "test" } }),
    );

    const { apiClient } = await loadApiClient();
    const result = await apiClient.get<{ id: number; name: string }>(
      "/users/1",
    );
    expect(result).toEqual({ id: 1, name: "test" });
  });

  it("throws ApiError on 401", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        { error: { code: "UNAUTHORIZED", message: "Invalid token" } },
        401,
      ),
    );

    const { apiClient, ApiError } = await loadApiClient();

    try {
      await apiClient.get("/me");
      expect.unreachable("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      const err = e as InstanceType<typeof ApiError>;
      expect(err.status).toBe(401);
      expect(err.code).toBe("UNAUTHORIZED");
      expect(err.message).toBe("Invalid token");
    }
  });

  it("handles non-JSON error response", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response("Internal Server Error", { status: 500 }),
    );

    const { apiClient, ApiError } = await loadApiClient();
    await expect(apiClient.get("/broken")).rejects.toThrow(ApiError);
  });
});

describe("accountScopedPath", () => {
  it("canonicalizes a UUID and appends only a relative resource path", async () => {
    const { accountScopedPath } = await loadApiClient();
    expect(
      accountScopedPath("550E8400-E29B-41D4-A716-446655440000", "/positions"),
    ).toBe("/exchange-accounts/550e8400-e29b-41d4-a716-446655440000/positions");
  });

  it("rejects a legacy realm or implicit default scope", async () => {
    const { accountScopedPath } = await loadApiClient();
    expect(() => accountScopedPath("default", "/positions")).toThrow(
      "canonical UUID",
    );
  });
});

describe("apiClient.post", () => {
  it("sends body and unwraps response", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ data: { id: 2 } }));

    const { apiClient } = await loadApiClient();
    const result = await apiClient.post<{ id: number }>("/items", {
      name: "new",
    });
    expect(result).toEqual({ id: 2 });

    const [, options] = mockFetch.mock.calls[0];
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ name: "new" });
  });
});

describe("apiClient.del", () => {
  it("sends DELETE request", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ data: { ok: true } }));

    const { apiClient } = await loadApiClient();
    const result = await apiClient.del<{ ok: boolean }>("/items/1");
    expect(result).toEqual({ ok: true });

    const [, options] = mockFetch.mock.calls[0];
    expect(options.method).toBe("DELETE");
  });
});

describe("apiClient.getList", () => {
  it("returns full response without unwrapping", async () => {
    const fullResponse = {
      data: [{ id: 1 }],
      pagination: { nextCursor: "abc" },
    };
    mockFetch.mockResolvedValueOnce(jsonResponse(fullResponse));

    const { apiClient } = await loadApiClient();
    const result = await apiClient.getList("/items");
    expect(result).toEqual(fullResponse);
  });
});

describe("request params", () => {
  it("appends query params to URL", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ data: [] }));

    const { apiClient } = await loadApiClient();
    await apiClient.get("/items", {
      params: { limit: "10", after: "abc" },
    });

    const url: string = mockFetch.mock.calls[0][0];
    expect(url).toContain("limit=10");
    expect(url).toContain("after=abc");
  });

  it("skips undefined params", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse({ data: [] }));

    const { apiClient } = await loadApiClient();
    await apiClient.get("/items", {
      params: { limit: "10", after: undefined },
    });

    const url: string = mockFetch.mock.calls[0][0];
    expect(url).toContain("limit=10");
    expect(url).not.toContain("after");
  });
});
