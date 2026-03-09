import { withSentryConfig } from "@sentry/nextjs";
import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";
import { withAxiom } from "next-axiom";

const nextConfig: NextConfig = {
	reactCompiler: true,
};

const withNextIntl = createNextIntlPlugin();

export default withSentryConfig(withAxiom(withNextIntl(nextConfig)), {
	org: process.env.SENTRY_ORG,
	project: process.env.SENTRY_PROJECT,
	silent: !process.env.CI,
	widenClientFileUpload: true,
	disableLogger: true,
	automaticVercelMonitors: true,
});
