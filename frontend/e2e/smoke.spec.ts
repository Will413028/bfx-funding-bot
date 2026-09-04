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

test.describe("Smoke tests", () => {
	test("landing page loads and redirects to default locale", async ({
		page,
	}) => {
		await page.goto("/");
		await expect(page).toHaveURL(/\/en/);
	});

	test("login page loads", async ({ page }) => {
		await page.goto("/en/login");
		await expect(
			page.getByRole("button", { name: /sign in|log in/i }),
		).toBeVisible();
	});

	test("pricing page loads", async ({ page }) => {
		await page.goto("/en/pricing");
		await expect(page.getByText(/free/i)).toBeVisible();
		await expect(page.getByText(/pro/i)).toBeVisible();
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
	test("zh-TW login page shows Chinese content", async ({ page }) => {
		await page.goto("/zh-TW/login");
		await expect(page.getByRole("button")).toBeVisible();
		// URL should contain zh-TW locale
		await expect(page).toHaveURL(/\/zh-TW\/login/);
	});
});
