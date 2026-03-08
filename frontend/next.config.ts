import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const nextConfig: NextConfig = {
  reactCompiler: true,
};

const withNextIntl = createNextIntlPlugin();

export default withNextIntl(nextConfig);

// Phase 3: Sentry 錯誤監控
// import { withSentryConfig } from "@sentry/nextjs";
// export default withSentryConfig(withNextIntl(nextConfig), {
//   org: process.env.SENTRY_ORG,
//   project: process.env.SENTRY_PROJECT,
//   silent: !process.env.CI,
//   widenClientFileUpload: true,
//   disableLogger: true,
//   automaticVercelMonitors: true,
// });
