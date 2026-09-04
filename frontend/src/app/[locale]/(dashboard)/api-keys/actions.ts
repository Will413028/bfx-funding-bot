"use server";

import { headers } from "next/headers";
import { accountScopedPath } from "@/lib/api-client";
import { auth, getOperatorMfaSessionAccess } from "@/lib/auth";
import type { ApiKey } from "@/types";

const API_URL = process.env.API_URL as string;

/**
 * Create a Bitfinex API key. The plaintext secret travels browser -> this
 * Server Action (server-side) -> the FastAPI web-API, NEVER through the client
 * /api/proxy hop. The web-API envelope-encrypts it before persisting.
 */
export async function createApiKeyAction(input: {
  exchangeAccountId: string;
  label: string;
  apiKey: string;
  apiSecret: string;
}): Promise<ApiKey> {
  // Validate the server-owned per-session MFA marker before minting a JWT or
  // sending the plaintext secret directly to the backend.
  const requestHeaders = await headers();
  const operatorUserId = process.env.BFX_OPERATOR_USER_ID?.trim();
  if (!operatorUserId) throw new Error("authTokenUnavailable");

  const access = await getOperatorMfaSessionAccess(
    requestHeaders,
    operatorUserId,
  );
  if (!access.allowed) throw new Error("authTokenUnavailable");

  // Mint the bearer token only after MFA authorization succeeds. If token
  // minting fails, never transmit the plaintext apiSecret unauthenticated.
  let token: string | undefined;
  try {
    const authRes = await auth.api.getToken({ headers: requestHeaders });
    token = authRes?.token;
  } catch {
    token = undefined;
  }
  if (!token) {
    // i18n key meaning "auth token unavailable, please retry" (retryable).
    throw new Error("authTokenUnavailable");
  }

  const { exchangeAccountId, ...payload } = input;
  const res = await fetch(
    new URL(
      `/api/v1${accountScopedPath(exchangeAccountId, "/credentials")}`,
      API_URL,
    ).toString(),
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload),
    },
  );

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
