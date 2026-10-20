// Next.js loads this in the browser before the app (Turbopack and webpack alike);
// @sentry/nextjs only injects the older sentry.client.config.ts under webpack.
import * as Sentry from "@sentry/nextjs";
import { sentryOptions } from "./src/lib/sentry";
import { SENTRY_TUNNEL_ROUTE } from "./src/lib/sentry-tunnel";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
	Sentry.init({
		...sentryOptions(dsn),
		// Same origin: the web API forwards to this DSN's project only (9d-5).
		tunnel: SENTRY_TUNNEL_ROUTE,
		replaysSessionSampleRate: 0,
		replaysOnErrorSampleRate: 0,
	});
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
