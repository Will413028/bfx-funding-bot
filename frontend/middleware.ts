import { getSessionCookie } from "better-auth/cookies";
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

const isDev = process.env.NODE_ENV === "development";

// Build connect-src with WebSocket backend origin (different from frontend in prod)
function buildConnectSrc(): string {
	const sources = ["'self'", "*.sentry.io", "*.ingest.sentry.io"];
	const wsUrl = process.env.NEXT_PUBLIC_WS_URL;
	if (wsUrl) {
		try {
			const url = new URL(wsUrl);
			// Allow the WS origin (wss:// or ws://)
			sources.push(url.origin);
			// Also allow the HTTPS variant for the TLS upgrade handshake
			if (url.protocol === "wss:") {
				sources.push(`https://${url.host}`);
			} else if (url.protocol === "ws:") {
				sources.push(`http://${url.host}`);
			}
		} catch {
			// Invalid URL — skip silently; the WS connection will fail at runtime
			// but at least the rest of the app won't break
		}
	}
	return `connect-src ${sources.join(" ")}`;
}

const cspDirectives = [
	"default-src 'self'",
	`script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
	"style-src 'self' 'unsafe-inline'",
	"img-src 'self' data: blob:",
	"font-src 'self'",
	buildConnectSrc(),
	"frame-ancestors 'none'",
	"base-uri 'self'",
	"form-action 'self'",
].join("; ");

const securityHeaders: Record<string, string> = {
	"Content-Security-Policy": cspDirectives,
	"X-Frame-Options": "DENY",
	"X-Content-Type-Options": "nosniff",
	"Referrer-Policy": "strict-origin-when-cross-origin",
	"Permissions-Policy": "camera=(), microphone=(), geolocation=()",
	"Strict-Transport-Security":
		"max-age=63072000; includeSubDomains; preload",
};

function applySecurityHeaders(response: NextResponse): NextResponse {
	for (const [key, value] of Object.entries(securityHeaders)) {
		response.headers.set(key, value);
	}
	return response;
}

export default function middleware(request: NextRequest) {
	const { pathname } = request.nextUrl;
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
		return applySecurityHeaders(NextResponse.redirect(loginUrl));
	}

	if (isAuthPage && isAuthenticated) {
		const locale =
			pathname.match(localePrefix)?.[1] || routing.defaultLocale;
		return applySecurityHeaders(
			NextResponse.redirect(new URL(`/${locale}/overview`, request.url)),
		);
	}

	return applySecurityHeaders(intlMiddleware(request));
}

export const config = {
	matcher: ["/((?!api|_next|.*\\..*).*)"],
};
