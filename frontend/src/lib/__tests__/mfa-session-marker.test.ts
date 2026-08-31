import type { PasskeyOptions } from "@better-auth/passkey";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const redis = vi.hoisted(() => ({
  get: vi.fn(),
  set: vi.fn(),
  del: vi.fn(),
}));

vi.mock("ioredis", () => ({
  default: class FakeRedis {
    get(key: string) {
      return redis.get(key);
    }

    set(key: string, value: string, mode: "EX", ttl: number) {
      return redis.set(key, value, mode, ttl);
    }

    del(key: string) {
      return redis.del(key);
    }
  },
}));

import * as authModule from "../auth";

type PasskeyAfterVerification = NonNullable<
  NonNullable<PasskeyOptions["authentication"]>["afterVerification"]
>;

function authAfterHook() {
  return authModule.auth.options.hooks?.after;
}

function passkeyAfterVerification(): PasskeyAfterVerification | undefined {
  const plugin = authModule.auth.options.plugins?.find(
    (candidate) => candidate.id === "passkey",
  );
  return plugin?.options?.authentication?.afterVerification as
    | PasskeyAfterVerification
    | undefined;
}

async function runAuthAfter(path: string, context: Record<string, unknown>) {
  const hook = authAfterHook();
  expect(hook).toBeDefined();
  if (!hook) return;
  const hookContext = { path, context, asResponse: false };
  await hook(hookContext);
}

async function runPasskeyVerification(context: object, userVerified: boolean) {
  const callback = passkeyAfterVerification();
  expect(callback).toBeDefined();
  if (!callback) return;

  const args = {
    ctx: { context },
    verification: { authenticationInfo: { userVerified } },
    clientData: {},
  } as Parameters<PasskeyAfterVerification>[0];
  await callback(args);
}

function newSession(token: string, expiresAt: Date) {
  return {
    session: { token, expiresAt },
    user: { id: "operator-1" },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-08-31T00:00:00Z"));
  redis.get.mockReset();
  redis.set.mockReset().mockResolvedValue("OK");
  redis.del.mockReset().mockResolvedValue(1);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("server-owned MFA session markers", () => {
  it("does not expose the backend JWT through Better Auth session responses", () => {
    const jwtPlugin = authModule.auth.options.plugins?.find(
      (candidate) => candidate.id === "jwt",
    );

    expect(jwtPlugin?.options?.disableSettingJwtHeader).toBe(true);
  });

  it("marks the new TOTP session with a TTL bounded by session expiry", async () => {
    const context = {
      newSession: newSession(
        "totp-session-token",
        new Date("2026-08-31T00:02:00Z"),
      ),
    };

    await runAuthAfter("/two-factor/verify-totp", context);

    expect(redis.set).toHaveBeenCalledWith(
      "bfx:mfa-verified:totp-session-token",
      "1",
      "EX",
      120,
    );
  });

  it.each(["/two-factor/verify-otp", "/two-factor/verify-backup-code"])(
    "marks a successful %s session",
    async (path) => {
      const context = {
        newSession: newSession(
          "secondary-mfa-session-token",
          new Date("2026-08-31T00:02:00Z"),
        ),
      };

      await runAuthAfter(path, context);

      expect(redis.set).toHaveBeenCalledWith(
        "bfx:mfa-verified:secondary-mfa-session-token",
        "1",
        "EX",
        120,
      );
    },
  );

  it("does not mark a passkey session when WebAuthn user verification is false", async () => {
    const context: Record<string, unknown> = { newSession: null };
    await runPasskeyVerification(context, false);
    context.newSession = newSession(
      "passkey-without-uv",
      new Date("2026-08-31T00:02:00Z"),
    );

    await runAuthAfter("/passkey/verify-authentication", context);

    expect(redis.set).not.toHaveBeenCalled();
  });

  it("marks only the passkey session created by a user-verified request", async () => {
    const context: Record<string, unknown> = { newSession: null };
    await runPasskeyVerification(context, true);
    context.newSession = newSession(
      "passkey-with-uv",
      new Date("2026-08-31T00:02:00Z"),
    );

    await runAuthAfter("/passkey/verify-authentication", context);

    expect(redis.set).toHaveBeenCalledWith(
      "bfx:mfa-verified:passkey-with-uv",
      "1",
      "EX",
      120,
    );
  });

  it("does not bind passkey proof to a session from another request context", async () => {
    const verifiedContext: Record<string, unknown> = { newSession: null };
    const otherContext: Record<string, unknown> = {
      newSession: newSession(
        "other-request-session",
        new Date("2026-08-31T00:02:00Z"),
      ),
    };
    await runPasskeyVerification(verifiedContext, true);

    await runAuthAfter("/passkey/verify-authentication", otherContext);

    expect(redis.set).not.toHaveBeenCalled();
  });

  it("recognizes only the exact session token stored in Redis", async () => {
    redis.get.mockImplementation(async (key: string) =>
      key === "bfx:mfa-verified:verified-token" ? "1" : null,
    );
    const isMfaVerifiedSession = Reflect.get(
      authModule,
      "isMfaVerifiedSession",
    );

    expect(isMfaVerifiedSession).toBeTypeOf("function");
    if (typeof isMfaVerifiedSession !== "function") return;
    await expect(isMfaVerifiedSession("verified-token")).resolves.toBe(true);
    await expect(isMfaVerifiedSession("other-token")).resolves.toBe(false);
  });

  it("allows an operator only when the current session token has a marker", async () => {
    const getSession = vi
      .spyOn(authModule.auth.api, "getSession")
      .mockResolvedValue({
        session: { token: "marked-session" },
        user: { id: "operator-1" },
      } as never);
    redis.get.mockResolvedValue("1");

    const getOperatorMfaSessionAccess = Reflect.get(
      authModule,
      "getOperatorMfaSessionAccess",
    );

    expect(getOperatorMfaSessionAccess).toBeTypeOf("function");
    if (typeof getOperatorMfaSessionAccess !== "function") return;

    await expect(
      getOperatorMfaSessionAccess(new Headers(), "operator-1"),
    ).resolves.toEqual({ allowed: true });
    expect(getSession).toHaveBeenCalledOnce();
    expect(getSession).toHaveBeenCalledWith({
      headers: expect.any(Headers),
      query: { disableCookieCache: true },
    });
    expect(redis.get).toHaveBeenCalledWith("bfx:mfa-verified:marked-session");
  });

  it("denies a pending two-factor sign-in before it can mint a backend token", async () => {
    vi.spyOn(authModule.auth.api, "getSession").mockResolvedValue(null);

    const getOperatorMfaSessionAccess = Reflect.get(
      authModule,
      "getOperatorMfaSessionAccess",
    );

    expect(getOperatorMfaSessionAccess).toBeTypeOf("function");
    if (typeof getOperatorMfaSessionAccess !== "function") return;

    await expect(
      getOperatorMfaSessionAccess(
        new Headers({ cookie: "better-auth.two_factor=signed-pending-mfa" }),
        "operator-1",
      ),
    ).resolves.toEqual({ allowed: false, error: "mfa_required" });
    expect(redis.get).not.toHaveBeenCalled();
  });

  it("fails closed when the marker lookup rejects", async () => {
    vi.spyOn(authModule.auth.api, "getSession").mockResolvedValue({
      session: { token: "session-with-unavailable-redis" },
      user: { id: "operator-1" },
    } as never);
    redis.get.mockRejectedValue(new Error("Redis unavailable"));

    const getOperatorMfaSessionAccess = Reflect.get(
      authModule,
      "getOperatorMfaSessionAccess",
    );

    expect(getOperatorMfaSessionAccess).toBeTypeOf("function");
    if (typeof getOperatorMfaSessionAccess !== "function") return;

    await expect(
      getOperatorMfaSessionAccess(new Headers(), "operator-1"),
    ).resolves.toEqual({ allowed: false, error: "mfa_required" });
  });
});
