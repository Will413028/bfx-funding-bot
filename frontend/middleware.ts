import createMiddleware from "next-intl/middleware";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { routing } from "@/i18n/routing";

const intlMiddleware = createMiddleware(routing);

const localePrefix = new RegExp(`^/(${routing.locales.join("|")})`);

const protectedPaths = [
  "/overview",
  "/api-keys",
  "/strategy",
  "/history",
  "/settings",
];
const authPaths = ["/login", "/register"];

export default function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const pathnameWithoutLocale = pathname.replace(localePrefix, "") || "/";

  const token = request.cookies.get("auth_token")?.value;
  const isProtected = protectedPaths.some((p) =>
    pathnameWithoutLocale.startsWith(p),
  );
  const isAuthPage = authPaths.some((p) =>
    pathnameWithoutLocale.startsWith(p),
  );

  if (isProtected && !token) {
    const locale =
      pathname.match(localePrefix)?.[1] || routing.defaultLocale;
    const loginUrl = new URL(`/${locale}/login`, request.url);
    loginUrl.searchParams.set("callbackUrl", pathnameWithoutLocale);
    return NextResponse.redirect(loginUrl);
  }

  if (isAuthPage && token) {
    const locale =
      pathname.match(localePrefix)?.[1] || routing.defaultLocale;
    return NextResponse.redirect(
      new URL(`/${locale}/overview`, request.url),
    );
  }

  return intlMiddleware(request);
}

export const config = {
  matcher: ["/((?!api|_next|.*\\..*).*)"],
};
