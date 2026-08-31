const INTERNAL_ORIGIN = "https://bfx-funding-bot.invalid";

/** Keep login and MFA redirects on this application origin. */
export function safeCallbackUrl(value: string | null): string {
  if (!value) return "/overview";
  try {
    const url = new URL(value, INTERNAL_ORIGIN);
    if (url.origin !== INTERNAL_ORIGIN || !url.pathname.startsWith("/")) {
      return "/overview";
    }
    return `${url.pathname}${url.search}`;
  } catch {
    return "/overview";
  }
}
