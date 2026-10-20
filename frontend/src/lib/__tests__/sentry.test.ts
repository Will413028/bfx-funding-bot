import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { scrubEvent, scrubText, sentryOptions } from "../sentry";
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

  it("is used by the browser init, the only runtime of a static export", () => {
    expect(read("instrumentation-client.ts")).toContain("sentryOptions(dsn)");
    for (const path of [
      "sentry.client.config.ts",
      "sentry.server.config.ts",
      "sentry.edge.config.ts",
      "instrumentation.ts",
    ]) {
      expect(existsSync(join(frontendRoot, path))).toBe(false);
    }
  });
});

describe("tunnel route", () => {
  it("is where the browser sends its events", () => {
    expect(read("instrumentation-client.ts")).toContain(
      "tunnel: SENTRY_TUNNEL_ROUTE",
    );
  });

  it("is the web API's /fe-events, which forwards to Sentry", () => {
    const ingress = read(
      "../backend/src/bfx_funding_bot/modules/api/ingress.py",
    );
    expect(ingress).toContain(`FE_EVENTS_PATH = "${SENTRY_TUNNEL_ROUTE}"`);
  });
});

describe("public build inputs", () => {
  // The UI is built inside the backend image (9d-5): backend/Dockerfile's `ui` stage.
  const dockerfile = read("../backend/Dockerfile");
  const release = read("../.github/workflows/release.yml");
  const publicKeys = ["NEXT_PUBLIC_SENTRY_DSN"];

  it("bakes the DSN and the release into the UI stage", () => {
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
    expect(release).toContain("build-contexts: frontend=frontend");
  });
});
