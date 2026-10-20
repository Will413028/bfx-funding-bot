// Error tracking for the browser (the UI is a static export since 9d-5). Off unless
// NEXT_PUBLIC_SENTRY_DSN was set at build time (release.yml passes it as a build arg).
// Events leave the operator's browser, so emails and IP addresses are scrubbed from
// every string, user, cookies and auth headers are dropped, and no trace headers are
// added to outgoing requests; an event that cannot be scrubbed is not sent. Notifications are decided in Sentry (new issue,
// regression); paging stays on symptoms.
const REDACTED = "[redacted]";
const EMAIL = /[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;
const IPV4 = /(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])/g;
const IPV6 =
  /(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}(?![0-9A-Za-z:])|(?<![0-9A-Za-z:])[0-9A-Fa-f:]*::[0-9A-Fa-f:]*[0-9A-Fa-f](?![0-9A-Za-z:])/g;
const DROPPED_HEADERS = new Set([
  "authorization",
  "cookie",
  "set-cookie",
  "x-forwarded-for",
  "x-real-ip",
  "cf-connecting-ip",
  "true-client-ip",
]);

export function scrubText(text: string): string {
  return text
    .replace(EMAIL, REDACTED)
    .replace(IPV4, REDACTED)
    .replace(IPV6, REDACTED);
}

function scrub(value: unknown): unknown {
  if (typeof value === "string") return scrubText(value);
  if (Array.isArray(value)) return value.map(scrub);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, scrub(item)]),
    );
  }
  return value;
}

type Scrubbable = {
  user?: unknown;
  request?: { cookies?: unknown; headers?: Record<string, string> };
};

export function scrubEvent<T extends object>(event: T): T | null {
  try {
    const copy: Scrubbable = { ...event };
    delete copy.user;
    if (copy.request) {
      const { cookies: _cookies, headers, ...request } = copy.request;
      copy.request = headers
        ? {
            ...request,
            headers: Object.fromEntries(
              Object.entries(headers).filter(
                ([key]) => !DROPPED_HEADERS.has(key.toLowerCase()),
              ),
            ),
          }
        : request;
    }
    return scrub(copy) as T;
  } catch {
    return null; // never send what could not be scrubbed
  }
}

export function sentryOptions(dsn: string) {
  return {
    dsn,
    release: process.env.NEXT_PUBLIC_SENTRY_RELEASE || undefined,
    environment: process.env.NODE_ENV === "production" ? "prod" : "development",
    sendDefaultPii: false,
    tracesSampleRate: 0,
    tracePropagationTargets: [],
    beforeSend: scrubEvent,
  };
}
