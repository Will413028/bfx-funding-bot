// Next.js reads the nonce out of the request's Content-Security-Policy header
// during dynamic rendering and stamps it on its own scripts, so script-src needs
// no 'unsafe-inline'. 'strict-dynamic' lets those nonce'd scripts load chunks.
export function buildContentSecurityPolicy(
  nonce: string,
  isDev: boolean,
): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    // Sentry ingest for the region our DSN lives in (o<org>.ingest.us.sentry.io).
    "connect-src 'self' https://*.ingest.us.sentry.io",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}

export function generateNonce(): string {
  return btoa(crypto.randomUUID());
}
