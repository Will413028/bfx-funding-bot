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

test.describe("Signup containment (full stack)", () => {
	// Needs the whole stack: Next dev server + standalone Python web-API +
	// dev Postgres (auth migration applied) + Upstash + JWKS env.
	// Gated so it doesn't fail spuriously in CI. See e2e/README.md.
	test.skip(
		!process.env.E2E_FULL_STACK,
		"requires full stack — see e2e/README.md",
	);

	test("direct email signup is rejected by the server hook", async ({
		request,
	}) => {
		const email = `e2e+${Date.now()}@example.com`;
		const password = "Sup3rSecret!";

		const response = await request.post("/api/auth/sign-up/email", {
			data: { email, password, name: "blocked signup" },
		});

		expect(response.status()).toBe(403);
		expect(await response.json()).toMatchObject({
			code: "signup_disabled",
			message: "signup_disabled",
		});
	});
});
