import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { APIError } from "better-auth";
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { auth } from "../auth";

const frontendRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../..",
);

function sourceFiles(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry.name) ? [path] : [];
  });
}

describe("self-service signup containment", () => {
  it("rejects direct email signup before the adapter can create a user", async () => {
    const create = vi.fn();
    const hook = auth.options.hooks?.before;

    expect(hook).toBeDefined();
    if (!hook) return;

    let error: unknown;
    try {
      const hookContext = {
        path: "/sign-up/email",
        context: { adapter: { create } },
        asResponse: false,
      };
      await hook(hookContext);
    } catch (caught) {
      error = caught;
    }

    expect(error).toBeInstanceOf(APIError);
    expect(error).toMatchObject({
      statusCode: 403,
      body: {
        code: "signup_disabled",
        message: "signup_disabled",
      },
    });
    expect(create).not.toHaveBeenCalled();
  });

  it("has no register route, component, link, or middleware surface", () => {
    const registerPage = join(
      frontendRoot,
      "src/app/[locale]/(auth)/register/page.tsx",
    );
    const registerForm = join(
      frontendRoot,
      "src/features/auth/components/register-form.tsx",
    );

    expect(existsSync(registerPage)).toBe(false);
    expect(existsSync(registerForm)).toBe(false);

    const surfaces = [
      ...sourceFiles(join(frontendRoot, "src/app/[locale]")),
      ...sourceFiles(join(frontendRoot, "src/features/auth")),
      join(frontendRoot, "middleware.ts"),
    ];

    for (const path of surfaces) {
      const source = readFileSync(path, "utf8");
      expect(source, path).not.toContain("/register");
      expect(source, path).not.toContain("register-form");
    }
  });
});

describe("execution proxy MFA boundary", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.doUnmock("next/headers");
    vi.doUnmock("@/lib/auth");
    vi.resetModules();
  });

  it("rejects an unverified operator before minting or forwarding a JWT", async () => {
    const getToken = vi.fn();
    const getOperatorMfaSessionAccess = vi
      .fn()
      .mockResolvedValue({ allowed: false, error: "mfa_required" });
    const backendFetch = vi.fn();
    vi.stubEnv("BFX_OPERATOR_USER_ID", "operator-1");
    vi.stubGlobal("fetch", backendFetch);
    vi.doMock("next/headers", () => ({
      headers: vi.fn(async () => new Headers()),
    }));
    vi.doMock("@/lib/auth", () => ({
      auth: { api: { getToken } },
      getOperatorMfaSessionAccess,
    }));

    const { GET } = await import("../../app/api/proxy/[...path]/route");
    const response = await GET(
      new NextRequest("http://localhost/api/proxy/config"),
      { params: Promise.resolve({ path: ["config"] }) },
    );

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({ error: "mfa_required" });
    expect(getOperatorMfaSessionAccess).toHaveBeenCalledOnce();
    expect(getToken).not.toHaveBeenCalled();
    expect(backendFetch).not.toHaveBeenCalled();
  });

  it("fails closed while Better Auth withholds the pre-MFA session", async () => {
    const getToken = vi.fn();
    const getOperatorMfaSessionAccess = vi
      .fn()
      .mockResolvedValue({ allowed: false, error: "mfa_required" });
    const backendFetch = vi.fn();
    vi.stubEnv("BFX_OPERATOR_USER_ID", "operator-1");
    vi.stubGlobal("fetch", backendFetch);
    vi.doMock("next/headers", () => ({
      headers: vi.fn(
        async () =>
          new Headers({ cookie: "better-auth.two_factor=signed-pending-mfa" }),
      ),
    }));
    vi.doMock("@/lib/auth", () => ({
      auth: { api: { getToken } },
      getOperatorMfaSessionAccess,
    }));

    const { GET } = await import("../../app/api/proxy/[...path]/route");
    const response = await GET(
      new NextRequest("http://localhost/api/proxy/config"),
      { params: Promise.resolve({ path: ["config"] }) },
    );

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({
      error: "mfa_required",
    });
    expect(getOperatorMfaSessionAccess).toHaveBeenCalledOnce();
    expect(getToken).not.toHaveBeenCalled();
    expect(backendFetch).not.toHaveBeenCalled();
  });

  it("allows a post-MFA operator session while keeping the JWT server-side", async () => {
    const getToken = vi.fn().mockResolvedValue({ token: "backend-jwt" });
    const getOperatorMfaSessionAccess = vi.fn().mockResolvedValue({
      allowed: true,
    });
    const backendFetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ data: { status: "ok" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubEnv("BFX_OPERATOR_USER_ID", "operator-1");
    vi.stubGlobal("fetch", backendFetch);
    vi.doMock("next/headers", () => ({
      headers: vi.fn(async () => new Headers()),
    }));
    vi.doMock("@/lib/auth", () => ({
      auth: { api: { getToken } },
      getOperatorMfaSessionAccess,
    }));

    const { GET } = await import("../../app/api/proxy/[...path]/route");
    const response = await GET(
      new NextRequest("http://localhost/api/proxy/config"),
      { params: Promise.resolve({ path: ["config"] }) },
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ data: { status: "ok" } });
    expect(getOperatorMfaSessionAccess).toHaveBeenCalledOnce();
    expect(getToken).toHaveBeenCalledOnce();
    expect(backendFetch).toHaveBeenCalledOnce();
    const forwarded = backendFetch.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(forwarded.headers).get("Authorization")).toBe(
      "Bearer backend-jwt",
    );
    expect(response.headers.has("Authorization")).toBe(false);
  });

  it("fails closed before reading a session when no operator is configured", async () => {
    const getToken = vi.fn();
    const getOperatorMfaSessionAccess = vi.fn();
    const backendFetch = vi.fn();
    vi.stubEnv("BFX_OPERATOR_USER_ID", "");
    vi.stubGlobal("fetch", backendFetch);
    vi.doMock("next/headers", () => ({
      headers: vi.fn(async () => new Headers()),
    }));
    vi.doMock("@/lib/auth", () => ({
      auth: { api: { getToken } },
      getOperatorMfaSessionAccess,
    }));

    const { GET } = await import("../../app/api/proxy/[...path]/route");
    const response = await GET(
      new NextRequest("http://localhost/api/proxy/config"),
      { params: Promise.resolve({ path: ["config"] }) },
    );

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({
      error: "auth_not_configured",
    });
    expect(getOperatorMfaSessionAccess).not.toHaveBeenCalled();
    expect(getToken).not.toHaveBeenCalled();
    expect(backendFetch).not.toHaveBeenCalled();
  });

  it("rejects a verified session that does not belong to the operator", async () => {
    const getToken = vi.fn().mockResolvedValue({ token: "backend-jwt" });
    const getOperatorMfaSessionAccess = vi
      .fn()
      .mockResolvedValue({ allowed: false, error: "operator_required" });
    const backendFetch = vi
      .fn()
      .mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubEnv("BFX_OPERATOR_USER_ID", "operator-1");
    vi.stubGlobal("fetch", backendFetch);
    vi.doMock("next/headers", () => ({
      headers: vi.fn(async () => new Headers()),
    }));
    vi.doMock("@/lib/auth", () => ({
      auth: { api: { getToken } },
      getOperatorMfaSessionAccess,
    }));

    const { GET } = await import("../../app/api/proxy/[...path]/route");
    const response = await GET(
      new NextRequest("http://localhost/api/proxy/config"),
      { params: Promise.resolve({ path: ["config"] }) },
    );

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({
      error: "operator_required",
    });
    expect(getOperatorMfaSessionAccess).toHaveBeenCalledOnce();
    expect(getToken).not.toHaveBeenCalled();
    expect(backendFetch).not.toHaveBeenCalled();
  });

  it("does not accept an MFA marker bound to a different session token", async () => {
    const getToken = vi.fn();
    const getOperatorMfaSessionAccess = vi
      .fn()
      .mockResolvedValue({ allowed: false, error: "mfa_required" });
    const backendFetch = vi.fn();
    vi.stubEnv("BFX_OPERATOR_USER_ID", "operator-1");
    vi.stubGlobal("fetch", backendFetch);
    vi.doMock("next/headers", () => ({
      headers: vi.fn(async () => new Headers()),
    }));
    vi.doMock("@/lib/auth", () => ({
      auth: { api: { getToken } },
      getOperatorMfaSessionAccess,
    }));

    const { GET } = await import("../../app/api/proxy/[...path]/route");
    const response = await GET(
      new NextRequest("http://localhost/api/proxy/config"),
      { params: Promise.resolve({ path: ["config"] }) },
    );

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({ error: "mfa_required" });
    expect(getOperatorMfaSessionAccess).toHaveBeenCalledOnce();
    expect(getToken).not.toHaveBeenCalled();
    expect(backendFetch).not.toHaveBeenCalled();
  });
});
