import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

// A static export has no server to render a page per request, so dashboard
// data must be fetched by the browser through the proxy (api-client).
const srcRoot = join(__dirname, "../..");
const dashboardRoot = join(srcRoot, "app/[locale]/(dashboard)");

// Better Auth TOTP enrollment reads the session server-side; it leaves with
// Better Auth when the web API serves the export (9d-5).
const HEADERS_EXCEPTIONS = [
  "app/[locale]/(dashboard)/settings/security/page.tsx",
];
// The proxy is the one server-side caller of the web API; env.ts validates it.
const API_URL_OWNERS = ["app/api/proxy/[...path]/route.ts", "lib/env.ts"];

function sourceFiles(root: string): string[] {
  return readdirSync(root, { recursive: true, encoding: "utf8" })
    .filter((path) => /\.(ts|tsx)$/.test(path) && !/\.test\.tsx?$/.test(path))
    .map((path) => relative(srcRoot, join(root, path)));
}

function read(path: string): string {
  return readFileSync(join(srcRoot, path), "utf8");
}

describe("dashboard data stays client-side", () => {
  it("no dashboard file imports next/headers", () => {
    const offenders = sourceFiles(dashboardRoot).filter(
      (path) =>
        !HEADERS_EXCEPTIONS.includes(path) &&
        /["']next\/headers["']/.test(read(path)),
    );
    expect(offenders).toEqual([]);
    for (const path of HEADERS_EXCEPTIONS) {
      expect(existsSync(join(srcRoot, path))).toBe(true);
    }
  });

  it("only the proxy reads API_URL", () => {
    const readers = sourceFiles(srcRoot).filter((path) =>
      /\bAPI_URL\b/.test(read(path)),
    );
    expect(readers.sort()).toEqual([...API_URL_OWNERS].sort());
  });
});
