import { withSentryConfig } from "@sentry/nextjs/config";
import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";
import { SENTRY_TUNNEL_ROUTE } from "./src/lib/sentry-tunnel";

const nextConfig: NextConfig = {
	reactCompiler: true,
	output: "standalone",
};

const withNextIntl = createNextIntlPlugin();

export default withSentryConfig(withNextIntl(nextConfig), {
	org: process.env.SENTRY_ORG,
	project: process.env.SENTRY_PROJECT,
	silent: !process.env.CI,
	widenClientFileUpload: true,
	tunnelRoute: SENTRY_TUNNEL_ROUTE,
	// Upload needs a SENTRY_AUTH_TOKEN in CI and is deferred; without it the maps
	// would only be served publicly next to the bundles.
	sourcemaps: { disable: true },
});
