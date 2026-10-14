// Browser events go to this same-origin path and the Next server forwards them to
// Sentry (withSentryConfig's tunnelRoute rewrite): content blockers drop requests
// to *.sentry.io. Kept fixed and neutral so middleware.ts can exclude it from the
// locale redirect; its matcher repeats the name because Next needs a literal there.
export const SENTRY_TUNNEL_ROUTE = "/fe-events";
