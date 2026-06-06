import { betterAuth } from "better-auth";
import { Pool } from "pg";
import { Redis } from "@upstash/redis";
import { admin, jwt, twoFactor } from "better-auth/plugins";
import { passkey } from "@better-auth/passkey";
import { nextCookies } from "better-auth/next-js";

// Lazy, globalThis-cached singletons.
//
// Why:
//  1. `Redis.fromEnv()` THROWS at import time if Upstash env vars are absent,
//     which breaks `next build` (the build never connects anywhere). Constructing
//     it lazily inside the secondaryStorage closures keeps import side-effect-free.
//  2. On serverless/HMR, the auth module is re-evaluated repeatedly. Caching the
//     pg Pool + Redis client on `globalThis` avoids leaking a new connection pool
//     on every re-eval (the documented Next-on-serverless singleton pattern).
const globalForAuth = globalThis as unknown as {
  _bfxAuthPool?: Pool;
  _bfxAuthRedis?: Redis;
};

function getPool(): Pool {
  // Same Neon DB, dedicated `auth` schema (D13). Pooled (-pooler) URL for serverless.
  globalForAuth._bfxAuthPool ??= new Pool({
    connectionString: process.env.DATABASE_URL,
    options: "-c search_path=auth",
  });
  return globalForAuth._bfxAuthPool;
}

function getRedis(): Redis {
  // UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN.
  // Constructed lazily so importing this module does not require Upstash env.
  globalForAuth._bfxAuthRedis ??= Redis.fromEnv();
  return globalForAuth._bfxAuthRedis;
}

export const auth = betterAuth({
  appName: "BFX Funding Bot",
  baseURL: process.env.BETTER_AUTH_URL,
  trustedOrigins: [process.env.BETTER_AUTH_URL!],

  database: getPool(),

  // Sessions + rate-limit counters live in Upstash (REST), not Neon.
  secondaryStorage: {
    get: async (key) => {
      const v = await getRedis().get<string>(key);
      return v ?? null;
    },
    set: async (key, value, ttl) => {
      if (ttl) await getRedis().set(key, value, { ex: ttl });
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

  // Route rate-limit counters to Upstash (secondaryStorage alone does NOT do this).
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

  plugins: [
    admin({ defaultRole: "user", adminRoles: ["admin"] }),
    twoFactor({ issuer: "BFX Funding Bot" }),
    passkey({
      rpID: process.env.PASSKEY_RP_ID!,
      rpName: "BFX Funding Bot",
      origin: process.env.BETTER_AUTH_URL!,
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
