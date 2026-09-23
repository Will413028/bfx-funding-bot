import { getSessionCookie } from "better-auth/cookies";
import createMiddleware from "next-intl/middleware";
import { NextRequest, NextResponse } from "next/server";
import { routing } from "@/i18n/routing";
import { buildContentSecurityPolicy, generateNonce } from "@/lib/csp";

const intlMiddleware = createMiddleware(routing);

const localePrefix = new RegExp(`^/(${routing.locales.join("|")})`);

const protectedPaths = [
	"/overview",
	"/api-keys",
	"/strategy",
	"/history",
	"/settings",
];
const authPaths = ["/login"];

const isDev = process.env.NODE_ENV === "development";

const securityHeaders: Record<string, string> = {
	"X-Frame-Options": "DENY",
	"X-Content-Type-Options": "nosniff",
	"Referrer-Policy": "strict-origin-when-cross-origin",
	"Permissions-Policy": "camera=(), microphone=(), geolocation=()",
	"Strict-Transport-Security":
		"max-age=63072000; includeSubDomains; preload",
};

function applySecurityHeaders(
	response: NextResponse,
	contentSecurityPolicy: string,
): NextResponse {
	response.headers.set("Content-Security-Policy", contentSecurityPolicy);
	for (const [key, value] of Object.entries(securityHeaders)) {
		response.headers.set(key, value);
	}
	return response;
}

export default function middleware(request: NextRequest) {
	const { pathname } = request.nextUrl;
	const nonce = generateNonce();
	const contentSecurityPolicy = buildContentSecurityPolicy(nonce, isDev);
	const pathnameWithoutLocale = pathname.replace(localePrefix, "") || "/";

	const sessionCookie = getSessionCookie(request);
	const isAuthenticated = !!sessionCookie;
	const isProtected = protectedPaths.some((p) =>
		pathnameWithoutLocale.startsWith(p),
	);
	const isAuthPage = authPaths.some((p) =>
		pathnameWithoutLocale.startsWith(p),
	);

	if (isProtected && !isAuthenticated) {
		const locale =
			pathname.match(localePrefix)?.[1] || routing.defaultLocale;
		const loginUrl = new URL(`/${locale}/login`, request.url);
		const callbackPath = pathnameWithoutLocale + request.nextUrl.search;
		loginUrl.searchParams.set("callbackUrl", callbackPath);
		return applySecurityHeaders(
			NextResponse.redirect(loginUrl),
			contentSecurityPolicy,
		);
	}

	if (isAuthPage && isAuthenticated) {
		const locale =
			pathname.match(localePrefix)?.[1] || routing.defaultLocale;
		return applySecurityHeaders(
			NextResponse.redirect(new URL(`/${locale}/overview`, request.url)),
			contentSecurityPolicy,
		);
	}

	// next-intl forwards the request headers it is given, so the nonce reaches
	// rendering, where Next.js applies it to its own scripts.
	const requestHeaders = new Headers(request.headers);
	requestHeaders.set("x-nonce", nonce);
	requestHeaders.set("Content-Security-Policy", contentSecurityPolicy);
	return applySecurityHeaders(
		intlMiddleware(new NextRequest(request, { headers: requestHeaders })),
		contentSecurityPolicy,
	);
}

export const config = {
	matcher: ["/((?!api|_next|.*\\..*).*)"],
};
