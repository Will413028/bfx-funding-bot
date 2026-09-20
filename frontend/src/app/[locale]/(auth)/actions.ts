"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { routing } from "@/i18n/routing";
import { auth } from "@/lib/auth";

interface AuthResult {
  success: boolean;
  error?: string;
  twoFactorRequired?: boolean;
}

export async function login(
  email: string,
  password: string,
): Promise<AuthResult> {
  try {
    const res = await auth.api.signInEmail({
      body: { email, password },
      asResponse: true,
    });
    if (!res.ok) return { success: false, error: "Invalid email or password" };
    const data = (await res.json().catch(() => ({}))) as {
      twoFactorRedirect?: boolean;
    };
    if (data.twoFactorRedirect)
      return { success: false, twoFactorRequired: true };
    return { success: true };
  } catch {
    return { success: false, error: "Login failed" };
  }
}

export async function logout(locale: string = "en") {
  const safeLocale = routing.locales.includes(
    locale as (typeof routing.locales)[number],
  )
    ? locale
    : routing.defaultLocale;
  try {
    await auth.api.signOut({ headers: await headers() });
  } catch {
    // best-effort
  }
  redirect(`/${safeLocale}/login`);
}

export async function logoutForEnrollmentRecovery(
  locale: string = "en",
): Promise<{ success: true; redirectTo: string } | { success: false }> {
  const safeLocale = routing.locales.includes(
    locale as (typeof routing.locales)[number],
  )
    ? locale
    : routing.defaultLocale;

  try {
    const response = await auth.api.signOut({
      headers: await headers(),
      asResponse: true,
    });
    if (!response.ok) return { success: false };
    const result = (await response.json().catch(() => null)) as {
      success?: boolean;
    } | null;
    if (result?.success !== true) return { success: false };
  } catch {
    return { success: false };
  }

  return { success: true, redirectTo: `/${safeLocale}/login` };
}
