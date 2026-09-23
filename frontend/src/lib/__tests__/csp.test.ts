import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import middleware from "../../../middleware";
import { buildContentSecurityPolicy, generateNonce } from "../csp";

function scriptSrc(policy: string): string {
  const directive = policy
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("script-src "));
  if (!directive) throw new Error(`no script-src in ${policy}`);
  return directive;
}

describe("buildContentSecurityPolicy", () => {
  it("allows scripts by nonce instead of unsafe-inline", () => {
    const directive = scriptSrc(buildContentSecurityPolicy("abc123", false));

    expect(directive).toContain("'nonce-abc123'");
    expect(directive).toContain("'strict-dynamic'");
    expect(directive).not.toContain("'unsafe-inline'");
    expect(directive).not.toContain("'unsafe-eval'");
  });

  it("adds unsafe-eval only in development", () => {
    expect(scriptSrc(buildContentSecurityPolicy("abc123", true))).toContain(
      "'unsafe-eval'",
    );
  });

  it("generates a fresh nonce per call", () => {
    expect(generateNonce()).not.toBe(generateNonce());
  });
});

describe("middleware CSP", () => {
  it("forwards the same nonce to rendering that the response enforces", () => {
    const response = middleware(new NextRequest("http://localhost/en"));

    const enforced = response.headers.get("Content-Security-Policy") ?? "";
    const nonce = /'nonce-([^']+)'/.exec(scriptSrc(enforced))?.[1];
    expect(nonce).toBeTruthy();
    // NextResponse.next({ request: { headers } }) surfaces forwarded request
    // headers this way; Next.js reads the nonce from the forwarded CSP.
    expect(response.headers.get("x-middleware-request-x-nonce")).toBe(nonce);
    expect(
      response.headers.get("x-middleware-request-content-security-policy"),
    ).toBe(enforced);
  });

  it("uses a different nonce on every request", () => {
    const first = middleware(new NextRequest("http://localhost/en"));
    const second = middleware(new NextRequest("http://localhost/en"));

    expect(first.headers.get("x-middleware-request-x-nonce")).not.toBe(
      second.headers.get("x-middleware-request-x-nonce"),
    );
  });

  it("still sets the policy on redirects", () => {
    const response = middleware(
      new NextRequest("http://localhost/en/overview"),
    );

    expect(response.status).toBe(307);
    expect(
      scriptSrc(response.headers.get("Content-Security-Policy") ?? ""),
    ).not.toContain("'unsafe-inline'");
  });
});
