// Next.js loads this in the browser before the app (Turbopack and webpack alike);
// @sentry/nextjs only injects the older sentry.client.config.ts under webpack.
import * as Sentry from "@sentry/nextjs";
import { sentryOptions } from "./src/lib/sentry";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
	Sentry.init({
		...sentryOptions(dsn),
		replaysSessionSampleRate: 0,
		replaysOnErrorSampleRate: 0,
	});
}

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
