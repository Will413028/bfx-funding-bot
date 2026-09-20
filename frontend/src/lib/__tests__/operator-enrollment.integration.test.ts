import { twoFactorClient } from "better-auth/client/plugins";
import { twoFactor } from "better-auth/plugins";
import { getTestInstance } from "better-auth/test";
import { beforeEach, describe, expect, it, vi } from "vitest";

const redis = vi.hoisted(() => ({
  values: new Map<string, string>(),
  get: vi.fn(async (key: string) => redis.values.get(key) ?? null),
  set: vi.fn(async (key: string, value: string) => {
    redis.values.set(key, value);
    return "OK";
  }),
  del: vi.fn(async (key: string) => (redis.values.delete(key) ? 1 : 0)),
}));
vi.mock("ioredis", () => ({
  default: class InMemoryRedis {
    get(key: string) {
      return redis.get(key);
    }

    set(key: string, value: string, _mode: "EX", _ttl: number) {
      return redis.set(key, value);
    }

    del(key: string) {
      return redis.del(key);
    }
  },
}));

import { auth as productionAuth } from "../auth";

function decodeBase32Secret(encoded: string): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let buffer = 0;
  let bits = 0;
  const bytes: number[] = [];
  for (const character of encoded) {
    const value = alphabet.indexOf(character);
    if (value < 0) throw new Error("invalid fixture TOTP secret");
    buffer = (buffer << 5) | value;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((buffer >> bits) & 0xff);
    }
  }
  return new TextDecoder().decode(Uint8Array.from(bytes));
}

async function createEnrollmentInstance() {
  const authAfter = productionAuth.options.hooks?.after;
  expect(authAfter).toBeDefined();
  if (!authAfter) throw new Error("fixture auth hook unavailable");

  const instance = await getTestInstance(
    {
      appName: "BFX Funding Bot fixture",
      hooks: { after: authAfter },
      plugins: [twoFactor({ issuer: "BFX Funding Bot fixture" })],
    },
    {
      clientOptions: { plugins: [twoFactorClient()] },
    },
  );
  const signedIn = await instance.signInWithTestUser();
  return { instance, signedIn };
}

async function enableAndGenerateCode(
  instance: Awaited<ReturnType<typeof createEnrollmentInstance>>["instance"],
) {
  const enabled = await instance.client.twoFactor.enable({
    password: instance.testUser.password,
  });
  expect(enabled.error).toBeNull();
  const secret = new URL(enabled.data?.totpURI ?? "").searchParams.get(
    "secret",
  );
  expect(secret).toBeTruthy();
  return await instance.auth.api.generateTOTP({
    body: { secret: decodeBase32Secret(secret ?? "") },
  });
}

describe("operator enrollment with real Better Auth endpoints", () => {
  beforeEach(() => {
    redis.values.clear();
    redis.get.mockClear();
    redis.set.mockClear();
    redis.del.mockClear();
  });

  it("does not mark a session after failed initial TOTP verification", async () => {
    const { instance, signedIn } = await createEnrollmentInstance();
    await signedIn.runWithUser(async (headers) => {
      const generated = await enableAndGenerateCode(instance);
      const invalidCode = generated.code.endsWith("0")
        ? `${generated.code.slice(0, -1)}1`
        : `${generated.code.slice(0, -1)}0`;

      const failed = await instance.client.twoFactor.verifyTotp({
        code: invalidCode,
        trustDevice: false,
      });
      expect(failed.error).not.toBeNull();
      expect(redis.set).not.toHaveBeenCalled();
      const afterFailure = await instance.auth.api.getSession({
        headers,
        query: { disableCookieCache: true },
      });
      expect(afterFailure?.user.twoFactorEnabled).not.toBe(true);
    });
  });

  it("marks only the rotated session after successful initial TOTP verification", async () => {
    const { instance, signedIn } = await createEnrollmentInstance();
    await signedIn.runWithUser(async (headers) => {
      const initial = await instance.auth.api.getSession({
        headers,
        query: { disableCookieCache: true },
      });
      expect(initial?.user.twoFactorEnabled).not.toBe(true);
      const generated = await enableAndGenerateCode(instance);
      expect(redis.set).not.toHaveBeenCalled();
      const oldHeaders = new Headers(headers);
      const verified = await instance.client.twoFactor.verifyTotp({
        code: generated.code,
        trustDevice: false,
        fetchOptions: {
          onSuccess: instance.sessionSetter(headers),
        },
      });
      expect(verified.error).toBeNull();

      const authoritative = await instance.auth.api.getSession({
        headers,
        query: { disableCookieCache: true },
      });
      expect(authoritative?.user.twoFactorEnabled).toBe(true);
      expect(authoritative?.session.token).not.toBe(initial?.session.token);
      await expect(
        instance.auth.api.getSession({
          headers: oldHeaders,
          query: { disableCookieCache: true },
        }),
      ).resolves.toBeNull();
      expect(redis.set).toHaveBeenCalledOnce();
      expect(redis.set).toHaveBeenCalledWith(
        `bfx:mfa-verified:${authoritative?.session.token}`,
        "1",
      );
    });
  });

  it("returns an expired session cookie and invalidates the session on server-action sign-out", async () => {
    const { instance, signedIn } = await createEnrollmentInstance();
    await signedIn.runWithUser(async (headers) => {
      expect(
        await instance.auth.api.getSession({
          headers,
          query: { disableCookieCache: true },
        }),
      ).not.toBeNull();
      const response = await instance.auth.api.signOut({
        headers,
        asResponse: true,
      });

      expect(response.ok).toBe(true);
      await expect(response.json()).resolves.toEqual({ success: true });
      expect(response.headers.get("set-cookie")).toContain(
        "better-auth.session_token=",
      );
      expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
      await expect(
        instance.auth.api.getSession({
          headers,
          query: { disableCookieCache: true },
        }),
      ).resolves.toBeNull();
    });
  });
});
