import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

// A static export (9d-5): the web API serves `out/` behind Tailscale Serve, so there is
// no Next server -- no middleware, route handlers, server actions or rewrites. Browser
// Sentry events go to the web API's /fe-events (instrumentation-client.ts).
const nextConfig: NextConfig = {
	reactCompiler: true,
	output: "export",
	trailingSlash: true,
	images: { unoptimized: true },
};

const withNextIntl = createNextIntlPlugin();

export default withNextIntl(nextConfig);
