// The only module that reads process.env. Next.js inlines these at build
// time, so each access must stay a literal `process.env.NAME` expression.

export const isDevelopment = process.env.NODE_ENV === "development";
export const isProduction = process.env.NODE_ENV === "production";
export const sentryRelease =
  process.env.NEXT_PUBLIC_SENTRY_RELEASE || undefined;
