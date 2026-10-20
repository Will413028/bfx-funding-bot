import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

// The UI is a static export (9d-5): nothing renders per request, so no file may read the
// request (next/headers, cookies), run on a server ("use server", route handlers) or read
// a server-side API address. Data is fetched by the browser from the web API (api-client).
const srcRoot = join(__dirname, "../..");
const frontendRoot = join(srcRoot, "..");

function sourceFiles(root: string): string[] {
  return readdirSync(root, { recursive: true, encoding: "utf8" })
    .filter((path) => /\.(ts|tsx)$/.test(path) && !/\.test\.tsx?$/.test(path))
    .map((path) => relative(srcRoot, join(root, path)));
}

function read(path: string): string {
  return readFileSync(join(srcRoot, path), "utf8");
}

describe("the UI is a static export", () => {
  it("no file reads the request or runs on a server", () => {
    const offenders = sourceFiles(srcRoot).filter((path) =>
      /["']next\/headers["']|["']use server["']|\bAPI_URL\b/.test(read(path)),
    );
    expect(offenders).toEqual([]);
  });

  it("has no route handlers or middleware", () => {
    const handlers = sourceFiles(join(srcRoot, "app")).filter((path) =>
      /(^|\/)route\.tsx?$/.test(path),
    );
    expect(handlers).toEqual([]);
    const top = readdirSync(frontendRoot);
    expect(top).not.toContain("middleware.ts");
    expect(top).not.toContain("instrumentation.ts");
  });

  it("is built as an export", () => {
    const config = readFileSync(join(frontendRoot, "next.config.ts"), "utf8");
    expect(config).toContain('output: "export"');
  });
});
