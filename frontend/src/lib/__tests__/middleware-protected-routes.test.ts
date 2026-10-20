import { readdirSync } from "node:fs";
import { join } from "node:path";
import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";
import middleware from "../../../middleware";

// Every route in the dashboard group needs a session; the middleware's list
// is written by hand, so check it against the routes that actually exist.
const dashboardRoot = join(__dirname, "../../app/[locale]/(dashboard)");
const dashboardRoutes = readdirSync(dashboardRoot, { withFileTypes: true })
  .filter((entry) => entry.isDirectory() && !entry.name.startsWith("__"))
  .map((entry) => `/${entry.name}`);

describe("middleware session guard", () => {
  it("finds the dashboard routes", () => {
    expect(dashboardRoutes).toContain("/overview");
  });

  it.each(dashboardRoutes)(
    "sends a request for %s without a session to login",
    (route) => {
      const response = middleware(
        new NextRequest(`http://localhost:3000/en${route}`),
      );

      expect(response.status).toBe(307);
      const location = new URL(response.headers.get("location") ?? "");
      expect(location.pathname).toBe("/en/login");
      expect(location.searchParams.get("callbackUrl")).toBe(route);
    },
  );
});
