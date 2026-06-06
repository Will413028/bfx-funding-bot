import { betterAuth } from "better-auth";
import { Pool } from "pg";
import { Redis } from "@upstash/redis";
import { admin, jwt, twoFactor } from "better-auth/plugins";
import { passkey } from "@better-auth/passkey";
import { nextCookies } from "better-auth/next-js";

const redis = Redis.fromEnv(); // UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN

export const auth = betterAuth({
  appName: "BFX Funding Bot",
  baseURL: process.env.BETTER_AUTH_URL,
  trustedOrigins: [process.env.BETTER_AUTH_URL!],

  // Same Neon DB, dedicated `auth` schema (D13). Pooled (-pooler) URL for serverless.
  database: new Pool({
    connectionString: process.env.DATABASE_URL,
    options: "-c search_path=auth",
  }),

  // Sessions + rate-limit counters live in Upstash (REST), not Neon.
  secondaryStorage: {
    get: async (key) => {
      const v = await redis.get<string>(key);
      return v ?? null;
    },
    set: async (key, value, ttl) => {
      if (ttl) await redis.set(key, value, { ex: ttl });
      else await redis.set(key, value);
    },
    delete: async (key) => {
      await redis.del(key);
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
        // Stable audience the Python backend checks, decoupled from the public URL.
        audience: "bfx-funding-backend",
        // issuer defaults to BETTER_AUTH_URL; expiration defaults to 15m. EdDSA default.
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
