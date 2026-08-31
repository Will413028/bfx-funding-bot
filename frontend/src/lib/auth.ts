import { type PasskeyOptions, passkey } from "@better-auth/passkey";
import { APIError, betterAuth } from "better-auth";
import { createAuthMiddleware } from "better-auth/api";
import { createCookieGetter } from "better-auth/cookies";
import { nextCookies } from "better-auth/next-js";
import { admin, jwt, twoFactor } from "better-auth/plugins";
import Redis from "ioredis";
import { Pool } from "pg";

// Lazy, globalThis-cached singletons (pg Pool + Redis).
//
// On serverless/HMR the auth module is re-evaluated repeatedly; caching the
// clients on `globalThis` avoids leaking a new pool/connection per re-eval.
// Both are constructed lazily (inside the getters) so importing this module is
// side-effect-free and `next build` never connects anywhere.
const globalForAuth = globalThis as unknown as {
  _bfxAuthPool?: Pool;
  _bfxAuthRedis?: Redis;
};

function getPool(): Pool {
  // VM-local Postgres, dedicated `auth` schema. Direct (no -pooler) so the
  // `-c search_path=auth` startup option is honored.
  globalForAuth._bfxAuthPool ??= new Pool({
    connectionString: process.env.DATABASE_URL,
    options: "-c search_path=auth",
  });
  return globalForAuth._bfxAuthPool;
}

function getRedis(): Redis {
  // VM-local Redis (docker service `redis`), standard protocol via ioredis.
  // biome-ignore lint/style/noNonNullAssertion: required server env, validated in lib/env.ts.
  globalForAuth._bfxAuthRedis ??= new Redis(process.env.REDIS_URL!);
  return globalForAuth._bfxAuthRedis;
}

const MFA_VERIFIED_PREFIX = "bfx:mfa-verified:";
const TWO_FACTOR_VERIFICATION_PATHS = new Set([
  "/two-factor/verify-totp",
  "/two-factor/verify-otp",
  "/two-factor/verify-backup-code",
]);

interface MfaMarkerStorage {
  get(key: string): Promise<string | null>;
  set(key: string, value: string, ttlSeconds: number): Promise<void>;
}

const redisMfaMarkerStorage: MfaMarkerStorage = {
  get: async (key) => await getRedis().get(key),
  set: async (key, value, ttlSeconds) => {
    await getRedis().set(key, value, "EX", ttlSeconds);
  },
};

type PasskeyAfterVerification = NonNullable<
  NonNullable<PasskeyOptions["authentication"]>["afterVerification"]
>;

function createMfaVerificationHandlers(
  storage: MfaMarkerStorage = redisMfaMarkerStorage,
) {
  const userVerifiedPasskeyContexts = new WeakSet<object>();

  const passkeyAfterVerification: PasskeyAfterVerification = async ({
    ctx,
    verification,
  }) => {
    if (verification.authenticationInfo.userVerified === true) {
      userVerifiedPasskeyContexts.add(ctx.context);
    }
  };

  const authAfter = createAuthMiddleware(async (ctx) => {
    const isTwoFactorVerification = TWO_FACTOR_VERIFICATION_PATHS.has(ctx.path);
    const isUserVerifiedPasskey =
      ctx.path === "/passkey/verify-authentication" &&
      userVerifiedPasskeyContexts.delete(ctx.context);
    if (!isTwoFactorVerification && !isUserVerifiedPasskey) return;

    const session = ctx.context.newSession?.session;
    if (!session) return;

    const ttlSeconds = Math.floor(
      (session.expiresAt.getTime() - Date.now()) / 1000,
    );
    if (ttlSeconds <= 0) return;

    await storage.set(
      `${MFA_VERIFIED_PREFIX}${session.token}`,
      "1",
      ttlSeconds,
    );
  });

  return { authAfter, passkeyAfterVerification };
}

const mfaVerification = createMfaVerificationHandlers();

export async function isMfaVerifiedSession(
  sessionToken: string,
  storage: MfaMarkerStorage = redisMfaMarkerStorage,
): Promise<boolean> {
  return (await storage.get(`${MFA_VERIFIED_PREFIX}${sessionToken}`)) === "1";
}

export type OperatorMfaSessionAccess =
  | { allowed: true }
  | { allowed: false; error: "mfa_required" | "operator_required" };

export const rejectSelfServiceSignup = createAuthMiddleware(async (ctx) => {
  if (ctx.path === "/sign-up/email") {
    throw new APIError("FORBIDDEN", {
      code: "signup_disabled",
      message: "signup_disabled",
    });
  }
});

export const auth = betterAuth({
  appName: "BFX Funding Bot",
  baseURL: process.env.BETTER_AUTH_URL,
  // biome-ignore lint/style/noNonNullAssertion: required server env, validated in lib/env.ts; not imported here to keep this module build-safe (env.ts process.exit(1)s on missing vars).
  trustedOrigins: [process.env.BETTER_AUTH_URL!],

  database: getPool(),

  // Sessions + rate-limit counters live in VM-local Redis (ioredis), not Postgres.
  secondaryStorage: {
    get: async (key) => {
      return await getRedis().get(key); // ioredis returns string | null
    },
    set: async (key, value, ttl) => {
      if (ttl) await getRedis().set(key, value, "EX", ttl);
      else await getRedis().set(key, value);
    },
    delete: async (key) => {
      await getRedis().del(key);
    },
  },

  emailAndPassword: {
    enabled: true,
    autoSignIn: true,
    requireEmailVerification: false, // v1
  },

  session: {
    expiresIn: 60 * 60 * 24 * 7,
    updateAge: 60 * 60 * 24,
    cookieCache: { enabled: true, maxAge: 5 * 60 },
  },

  // Route rate-limit counters to Redis (secondaryStorage alone does NOT do this).
  rateLimit: {
    enabled: true,
    storage: "secondary-storage",
    customRules: {
      "/sign-in/email": { window: 60, max: 5 },
      "/sign-up/email": { window: 60, max: 5 },
      "/two-factor/*": { window: 60, max: 5 },
      "/forget-password": { window: 60, max: 3 },
    },
  },

  hooks: {
    before: rejectSelfServiceSignup,
    after: mfaVerification.authAfter,
  },

  plugins: [
    admin({ defaultRole: "user", adminRoles: ["admin"] }),
    twoFactor({ issuer: "BFX Funding Bot" }),
    passkey({
      // biome-ignore lint/style/noNonNullAssertion: required server env, validated in lib/env.ts.
      rpID: process.env.PASSKEY_RP_ID!,
      rpName: "BFX Funding Bot",
      // biome-ignore lint/style/noNonNullAssertion: required server env, validated in lib/env.ts.
      origin: process.env.BETTER_AUTH_URL!,
      authentication: {
        afterVerification: mfaVerification.passkeyAfterVerification,
      },
    }),
    jwt({
      jwt: {
        // Stable issuer + audience the Python backend verifies, PINNED so both are
        // decoupled from the public URL — a deploy/domain change can't break JWT
        // verification (BE `better_auth_issuer` default must byte-match this issuer).
        // NOTE: this is a plain (non-URL) string — fine for our closed FE↔BE JWKS
        // contract (PyJWT does string-equality). If `oidc-provider`/`mcp` is ever
        // added, this becomes the OIDC discovery issuer and MUST be an https:// URL.
        issuer: "bfx-funding-bot",
        audience: "bfx-funding-backend",
        // expiration defaults to 15m; EdDSA default.
        definePayload: ({ user }) => ({
          sub: user.id,
          email: user.email,
          role: (user as { role?: string }).role ?? "user",
        }),
      },
    }),
    // MUST be last: propagates Better Auth Set-Cookie from server actions to the browser.
    nextCookies(),
  ],
});

function hasCookie(requestHeaders: Headers, name: string): boolean {
  return (
    requestHeaders
      .get("cookie")
      ?.split(";")
      .some((cookie) => cookie.trim().startsWith(`${name}=`)) ?? false
  );
}

/**
 * Authorizes an execution-capable request from server-owned session state.
 * A user enrollment flag is deliberately not considered proof of the current
 * session's MFA challenge; only the marker keyed by its exact session token is.
 */
export async function getOperatorMfaSessionAccess(
  requestHeaders: Headers,
  operatorUserId: string,
): Promise<OperatorMfaSessionAccess> {
  let session: Awaited<ReturnType<typeof auth.api.getSession>> | null = null;
  try {
    session = await auth.api.getSession({ headers: requestHeaders });
  } catch {
    session = null;
  }

  if (!session) {
    const pendingMfaCookie = createCookieGetter(auth.options)("two_factor");
    return {
      allowed: false,
      error: hasCookie(requestHeaders, pendingMfaCookie.name)
        ? "mfa_required"
        : "operator_required",
    };
  }

  if (session.user.id !== operatorUserId) {
    return { allowed: false, error: "operator_required" };
  }

  try {
    if (await isMfaVerifiedSession(session.session.token)) {
      return { allowed: true };
    }
  } catch {
    // Redis marker lookup is part of the authorization decision: unavailable
    // storage must never turn into an execution-capable request.
  }
  return { allowed: false, error: "mfa_required" };
}
