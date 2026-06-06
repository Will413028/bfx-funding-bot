import { expect, test } from "@playwright/test";

test.describe("Protected-route guard", () => {
	// Runs with the FE dev server alone — exercises the `getSessionCookie`
	// middleware check. No backend / Postgres / Upstash required.
	test("unauthenticated visit to /en/overview redirects to /en/login", async ({
		page,
	}) => {
		await page.goto("/en/overview");
		// Middleware redirects protected paths to the login page (a callbackUrl
		// query param is appended, so match the path prefix, not an exact URL).
		await expect(page).toHaveURL(/\/en\/login/);
	});
});

test.describe("Auth happy path (full stack)", () => {
	// Needs the whole stack: Next dev server + standalone Python web-API +
	// dev Postgres (auth migration applied) + Upstash + JWKS env.
	// Gated so it doesn't fail spuriously in CI. See e2e/README.md.
	test.skip(
		!process.env.E2E_FULL_STACK,
		"requires full stack — see e2e/README.md",
	);

	test("sign up, land on overview, authenticated proxy GET returns 200", async ({
		page,
	}) => {
		const email = `e2e+${Date.now()}@example.com`;
		const password = "Sup3rSecret!";

		await page.goto("/en/register");

		await page.getByLabel(/email/i).fill(email);
		await page.getByLabel(/password/i).fill(password);

		// `autoSignIn: true` mints a session on sign-up; the form then routes to
		// /overview. Set up the response wait before submitting to avoid a race.
		const proxyGet = page.waitForResponse(
			(r) =>
				r.url().includes("/api/proxy/") && r.request().method() === "GET",
		);

		await page
			.getByRole("button", { name: /sign ?up|register/i })
			.click();

		// Authenticated dashboard loads at the localized overview route.
		await expect(page).toHaveURL(/\/en\/overview/);

		// The dashboard fires authenticated GETs through the proxy; a 200 proves
		// the Better-Auth session cookie → server-minted backend JWT chain works.
		const resp = await proxyGet;
		expect(resp.status()).toBe(200);
	});
});
