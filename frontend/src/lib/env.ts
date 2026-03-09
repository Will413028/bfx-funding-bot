import { z } from "zod";

const clientEnvSchema = z.object({
  NEXT_PUBLIC_APP_URL: z.string().url(),
  NEXT_PUBLIC_APP_NAME: z.string().min(1),
  NEXT_PUBLIC_WS_URL: z.string().min(1),
  NEXT_PUBLIC_SENTRY_DSN: z.string().url().optional(),
});

const serverEnvSchema = clientEnvSchema.extend({
  API_URL: z.string().url(),
  AUTH_SECRET: z.string().min(1),
});

function parseEnv() {
  if (typeof window !== "undefined") {
    const result = clientEnvSchema.safeParse({
      NEXT_PUBLIC_APP_URL: process.env.NEXT_PUBLIC_APP_URL,
      NEXT_PUBLIC_APP_NAME: process.env.NEXT_PUBLIC_APP_NAME,
      NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL,
      NEXT_PUBLIC_SENTRY_DSN: process.env.NEXT_PUBLIC_SENTRY_DSN,
    });
    if (!result.success) {
      console.error(
        "[env] Client environment validation failed:",
        result.error.flatten().fieldErrors,
      );
      return clientEnvSchema.parse({});
    }
    return result.data;
  }

  const result = serverEnvSchema.safeParse(process.env);
  if (!result.success) {
    console.error(
      "[env] Server environment validation failed:",
      result.error.flatten().fieldErrors,
    );
    process.exit(1);
  }
  return result.data;
}

export const env = parseEnv();
