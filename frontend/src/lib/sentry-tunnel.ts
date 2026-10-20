// Browser events go to this same-origin path and the web API forwards them to the
// configured Sentry project (backend modules/api/ingress.py, FE_EVENTS_PATH): content
// blockers drop requests to *.sentry.io.
export const SENTRY_TUNNEL_ROUTE = "/fe-events";
