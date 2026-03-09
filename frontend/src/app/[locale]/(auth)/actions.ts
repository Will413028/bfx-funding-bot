"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { routing } from "@/i18n/routing";

// eslint-disable-next-line -- server-only, validated by env.ts at startup
const API_URL = process.env.API_URL as string;
const IS_PRODUCTION = process.env.NODE_ENV === "production";

interface AuthResult {
  success: boolean;
  error?: string;
}

async function setAuthCookie(token: string) {
  (await cookies()).set("auth_token", token, {
    httpOnly: true,
    secure: IS_PRODUCTION,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 7, // 7 days
  });
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

  const data = body.data as { token: string };
  await setAuthCookie(data.token);
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
  const safeLocale = routing.locales.includes(locale as typeof routing.locales[number]) ? locale : routing.defaultLocale;
  (await cookies()).delete("auth_token");
  redirect(`/${safeLocale}/login`);
}
