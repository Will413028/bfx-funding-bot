"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { routing } from "@/i18n/routing";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;
const IS_PRODUCTION = process.env.NODE_ENV === "production";

const ACCESS_TOKEN_MAX_AGE = 15 * 60; // 15 minutes
const REFRESH_TOKEN_MAX_AGE = 60 * 60 * 24 * 7; // 7 days

interface AuthResult {
  success: boolean;
  error?: string;
}

async function setAuthCookies(accessToken: string, refreshToken: string) {
  const jar = await cookies();
  jar.set("auth_token", accessToken, {
    httpOnly: true,
    secure: IS_PRODUCTION,
    sameSite: "lax",
    path: "/",
    maxAge: ACCESS_TOKEN_MAX_AGE,
  });
  jar.set("refresh_token", refreshToken, {
    httpOnly: true,
    secure: IS_PRODUCTION,
    sameSite: "strict",
    path: "/api",
    maxAge: REFRESH_TOKEN_MAX_AGE,
  });
}

async function clearAuthCookies() {
  const jar = await cookies();
  jar.delete("auth_token");
  jar.delete({ name: "refresh_token", path: "/api" });
}

export async function login(
  email: string,
  password: string,
): Promise<AuthResult> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
  } catch {
    return { success: false, error: "Network error" };
  }

  let body: Record<string, unknown>;
  try {
    body = await res.json();
  } catch {
    return { success: false, error: "Login failed" };
  }

  if (!res.ok) {
    const err = body.error as { message?: string } | undefined;
    return {
      success: false,
      error: err?.message ?? "Login failed",
    };
  }

  const data = body.data as {
    accessToken: string;
    refreshToken: string;
  };
  await setAuthCookies(data.accessToken, data.refreshToken);
  return { success: true };
}

export async function register(
  email: string,
  password: string,
): Promise<AuthResult> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
  } catch {
    return { success: false, error: "Network error" };
  }

  let body: Record<string, unknown>;
  try {
    body = await res.json();
  } catch {
    return { success: false, error: "Registration failed" };
  }

  if (!res.ok) {
    const err = body.error as { message?: string } | undefined;
    return {
      success: false,
      error: err?.message ?? "Registration failed",
    };
  }

  // Auto-login after successful registration
  return login(email, password);
}

export async function logout(locale: string = "en") {
  const safeLocale = routing.locales.includes(
    locale as (typeof routing.locales)[number],
  )
    ? locale
    : routing.defaultLocale;

  // Best-effort: call backend logout (revoke refresh token)
  try {
    const token = (await cookies()).get("auth_token")?.value;
    if (token) {
      await fetch(`${API_URL}/api/v1/auth/logout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
    }
  } catch {
    // ignore — cookie deletion is the primary mechanism
  }

  await clearAuthCookies();
  redirect(`/${safeLocale}/login`);
}
