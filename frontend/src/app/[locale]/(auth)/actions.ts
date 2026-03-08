"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

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
  const res = await fetch(`${API_URL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  const body = await res.json();

  if (!res.ok) {
    return {
      success: false,
      error: body.error?.message ?? "Login failed",
    };
  }

  await setAuthCookie(body.data.token);
  return { success: true };
}

export async function register(
  email: string,
  password: string,
): Promise<AuthResult> {
  const res = await fetch(`${API_URL}/api/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  const body = await res.json();

  if (!res.ok) {
    return {
      success: false,
      error: body.error?.message ?? "Registration failed",
    };
  }

  // Auto-login after successful registration
  return login(email, password);
}

export async function logout() {
  (await cookies()).delete("auth_token");
  redirect("/login");
}
