import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({ headers: async () => new Headers() }));
vi.mock("@/lib/auth", () => ({
  auth: { api: { getToken: vi.fn(async () => ({ token: "jwt-123" })) } },
}));

import { createApiKeyAction } from "@/app/[locale]/(dashboard)/api-keys/actions";

afterEach(() => vi.restoreAllMocks());

describe("createApiKeyAction", () => {
  it("posts plaintext server-side with bearer and returns data", async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () =>
        new Response(
          JSON.stringify({
            data: {
              id: "1",
              label: "main",
              apiKey: "PUB",
              apiSecret: "****",
              exchangeStatus: "unverified",
              createdAt: "x",
            },
          }),
          { status: 201 },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await createApiKeyAction({
      label: "main",
      apiKey: "PUB",
      apiSecret: "SEC",
    });

    expect(result.apiKey).toBe("PUB");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/api-keys");
    expect((init as RequestInit).method).toBe("POST");
    expect((init as RequestInit).body).toContain("SEC"); // plaintext only in server fetch body
    // biome-ignore lint/suspicious/noExplicitAny: test-only header access
    expect((init as any).headers.Authorization).toBe("Bearer jwt-123");
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
      createApiKeyAction({ label: "x", apiKey: "P", apiSecret: "S" }),
    ).rejects.toThrow("key_already_exists");
  });
});
