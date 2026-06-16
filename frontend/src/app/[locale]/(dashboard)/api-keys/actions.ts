"use server";

import { headers } from "next/headers";
import { auth } from "@/lib/auth";
import type { ApiKey } from "@/types";

const API_URL = process.env.API_URL as string;

/**
 * Create a Bitfinex API key. The plaintext secret travels browser -> this
 * Server Action (server-side) -> the FastAPI web-API, NEVER through the client
 * /api/proxy hop. The web-API envelope-encrypts it before persisting.
 */
export async function createApiKeyAction(input: {
  label: string;
  apiKey: string;
  apiSecret: string;
}): Promise<ApiKey> {
  // Mint the bearer token BEFORE issuing the secret-bearing request. If the
  // token is unavailable (getToken throws or returns none) we MUST fail fast
  // and never transmit the plaintext apiSecret on an unauthenticated request.
  let token: string | undefined;
  try {
    const authRes = await auth.api.getToken({ headers: await headers() });
    token = authRes?.token;
  } catch {
    token = undefined;
  }
  if (!token) {
    // i18n key meaning "auth token unavailable, please retry" (retryable).
    throw new Error("authTokenUnavailable");
  }

  const res = await fetch(new URL("/api/v1/api-keys", API_URL).toString(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(input),
  });

  const text = await res.text();
  if (!res.ok) {
    let message = "Failed to save API key";
    try {
      const parsed = JSON.parse(text) as {
        detail?: string;
        error?: { message?: string };
      };
      message = parsed.detail ?? parsed.error?.message ?? message;
    } catch {
      /* keep default */
    }
    throw new Error(message);
  }
  return (JSON.parse(text) as { data: ApiKey }).data;
}
