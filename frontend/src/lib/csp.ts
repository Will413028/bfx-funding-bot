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
    "connect-src 'self' *.sentry.io *.ingest.sentry.io",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}

export function generateNonce(): string {
  return btoa(crypto.randomUUID());
}
