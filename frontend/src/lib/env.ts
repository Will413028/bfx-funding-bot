import { z } from "zod";

const clientEnvSchema = z.object({
  NEXT_PUBLIC_APP_URL: z.string().url(),
  NEXT_PUBLIC_APP_NAME: z.string().min(1),
  NEXT_PUBLIC_WS_URL: z.string().min(1),
});

const serverEnvSchema = clientEnvSchema.extend({
  API_URL: z.string().url(),
  AUTH_SECRET: z.string().min(1),
});

export const env =
  typeof window === "undefined"
    ? serverEnvSchema.parse(process.env)
    : clientEnvSchema.parse({
        NEXT_PUBLIC_APP_URL: process.env.NEXT_PUBLIC_APP_URL,
        NEXT_PUBLIC_APP_NAME: process.env.NEXT_PUBLIC_APP_NAME,
        NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL,
      });
