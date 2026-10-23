import { expect, test } from "@playwright/test";

// The full-stack smoke environment may replace this with the UUID seeded in its
// disposable database.  There is intentionally no realm/default account
// fallback in private URLs.
const SEEDED_EXCHANGE_ACCOUNT_ID =
	process.env.E2E_EXCHANGE_ACCOUNT_ID ??
	"550e8400-e29b-41d4-a716-446655440000";

function accountScopedPath(resourcePath: `/${string}`): string {
	return `/exchange-accounts/${SEEDED_EXCHANGE_ACCOUNT_ID.toLowerCase()}${resourcePath}`;
}

// `/` has no page in the static export: the web API redirects it to the default
// locale (backend/tests/modules/api/test_ingress.py), so `next dev` cannot test it.
test.describe("Smoke tests", () => {
	test("overview page loads", async ({ page }) => {
		await page.goto("/en/overview/");
		await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
	});
});

test.describe("ExchangeAccount identity contract", () => {
	test("keeps private URLs explicitly scoped to the seeded UUID", () => {
		expect(SEEDED_EXCHANGE_ACCOUNT_ID).toMatch(
			/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
		);
		expect(accountScopedPath("/positions")).toBe(
			`/exchange-accounts/${SEEDED_EXCHANGE_ACCOUNT_ID.toLowerCase()}/positions`,
		);
	});
});

test.describe("i18n", () => {
	test("zh-TW overview keeps its locale", async ({ page }) => {
		await page.goto("/zh-TW/overview/");
		await expect(page.getByRole("navigation", { name: "Main navigation" })).toBeVisible();
		await expect(page).toHaveURL(/\/zh-TW\/overview\//);
	});
});
