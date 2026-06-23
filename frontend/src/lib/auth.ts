import { passkey } from "@better-auth/passkey";
import { betterAuth } from "better-auth";
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

  plugins: [
    admin({ defaultRole: "user", adminRoles: ["admin"] }),
    twoFactor({ issuer: "BFX Funding Bot" }),
    passkey({
      // biome-ignore lint/style/noNonNullAssertion: required server env, validated in lib/env.ts.
      rpID: process.env.PASSKEY_RP_ID!,
      rpName: "BFX Funding Bot",
      // biome-ignore lint/style/noNonNullAssertion: required server env, validated in lib/env.ts.
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
