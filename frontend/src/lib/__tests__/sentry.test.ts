import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { scrubEvent, scrubText, secretValues, sentryOptions } from "../sentry";
import { SENTRY_TUNNEL_ROUTE } from "../sentry-tunnel";

const frontendRoot = join(__dirname, "../../..");
const read = (path: string) => readFileSync(join(frontendRoot, path), "utf8");

const EMAIL = "operator@example.com";
const IPV4 = "203.0.113.7";
const IPV6 = "2001:db8:85a3::8a2e:370:7334";

describe("scrubEvent", () => {
  it("removes emails, IP addresses, user, cookies and auth headers", () => {
    const detail = `login failed for ${EMAIL} from ${IPV4} / ${IPV6}`;
    const scrubbed = scrubEvent({
      message: detail,
      exception: { values: [{ type: "Error", value: detail }] },
      breadcrumbs: [{ message: detail }],
      user: { email: EMAIL, ip_address: IPV4 },
      request: {
        url: "https://example.invalid/en/overview",
        cookies: { session: "s" },
        headers: {
          Authorization: "Bearer abc",
          Cookie: "session=s",
          "X-Forwarded-For": IPV4,
          Accept: "text/html",
        },
      },
    });

    const text = JSON.stringify(scrubbed);
    for (const leak of [EMAIL, IPV4, IPV6, "Bearer abc", "session=s"]) {
      expect(text).not.toContain(leak);
    }
    expect(scrubbed).not.toHaveProperty("user");
    expect(scrubbed?.request).toEqual({
      url: "https://example.invalid/en/overview",
      headers: { Accept: "text/html" },
    });
  });

  it("removes URL-encoded emails", () => {
    expect(scrubText("/login?email=operator%40example.com")).toBe(
      "/login?email=[redacted]",
    );
  });

  it("removes server secrets by value, including URL passwords", () => {
    const secrets = secretValues({
      BETTER_AUTH_SECRET: "fake-auth-secret-0123456789abcdef",
      DATABASE_URL: "postgresql://bfx_webauth:fake-db-pw@postgres:5432/bfx",
      REDIS_URL: undefined,
    });
    const text = scrubText(
      "secret fake-auth-secret-0123456789abcdef, connect failed: password fake-db-pw",
      secrets,
    );
    expect(text).not.toContain("fake-auth-secret");
    expect(text).not.toContain("fake-db-pw");
  });

  it("keeps ordinary text", () => {
    const text = "fUSD rate 0.00021 at 12:30:45, v1.2.3";
    expect(scrubText(text)).toBe(text);
  });

  it("drops an event it cannot scrub", () => {
    const event = { message: "x", extra: {} as Record<string, unknown> };
    Object.defineProperty(event.extra, "boom", {
      enumerable: true,
      get() {
        throw new Error("cannot read");
      },
    });
    expect(scrubEvent(event)).toBeNull();
  });
});

describe("sentryOptions", () => {
  it("never sends default PII or traces and always scrubs", () => {
    const options = sentryOptions("https://key@o1.ingest.us.sentry.io/1");
    expect(options.sendDefaultPii).toBe(false);
    expect(options.tracesSampleRate).toBe(0);
    expect(options.tracePropagationTargets).toEqual([]);
    expect(options.beforeSend).toBe(scrubEvent);
  });

  it("is used by every runtime's init", () => {
    for (const path of [
      "instrumentation-client.ts",
      "sentry.server.config.ts",
      "sentry.edge.config.ts",
    ]) {
      expect(read(path)).toContain("sentryOptions(dsn)");
    }
    // @sentry/nextjs injects this file only under webpack; Next 16 builds with Turbopack.
    expect(existsSync(join(frontendRoot, "sentry.client.config.ts"))).toBe(
      false,
    );
  });
});

describe("tunnel route", () => {
  it("is the route withSentryConfig rewrites to Sentry", () => {
    expect(read("next.config.ts")).toContain(
      "tunnelRoute: SENTRY_TUNNEL_ROUTE",
    );
  });

  it("is left alone by the locale middleware, which still covers pages", () => {
    const matcher = /matcher: \["([^"]+)"\]/.exec(read("middleware.ts"))?.[1];
    expect(matcher).toBeDefined();
    const matches = new RegExp(`^${matcher?.replaceAll("\\\\", "\\")}$`);
    expect(matches.test(SENTRY_TUNNEL_ROUTE)).toBe(false);
    expect(matches.test(`${SENTRY_TUNNEL_ROUTE}/`)).toBe(false);
    expect(matches.test("/en/overview")).toBe(true);
    expect(matches.test("/login")).toBe(true);
  });
});

describe("public build inputs", () => {
  const envSource = read("src/lib/env.ts");
  const schema = envSource.slice(
    envSource.indexOf("clientEnvSchema = z.object({"),
    envSource.indexOf("});"),
  );
  const publicKeys = [...schema.matchAll(/(NEXT_PUBLIC_[A-Z_]+):/g)].map(
    (match) => match[1],
  );
  const dockerfile = read("Dockerfile");
  const release = read("../.github/workflows/release.yml");

  it("covers the Sentry DSN", () => {
    expect(publicKeys).toContain("NEXT_PUBLIC_SENTRY_DSN");
  });

  it("bakes every public key and the release into the image", () => {
    for (const key of [...publicKeys, "NEXT_PUBLIC_SENTRY_RELEASE"]) {
      expect(dockerfile).toMatch(new RegExp(`^ARG ${key}$`, "m"));
      expect(dockerfile).toContain(`${key}=$${key}`);
    }
  });

  it("has the release workflow require and pass every public key", () => {
    const keys = /keys="([^"]+)"/.exec(release)?.[1].split(" ") ?? [];
    for (const key of publicKeys) {
      expect(keys).toContain(key);
      expect(release).toContain(`${key}: \${{ vars.${key} }}`);
    }
    expect(release).toContain(`echo "NEXT_PUBLIC_SENTRY_RELEASE=\${REVISION}"`);
  });
});
