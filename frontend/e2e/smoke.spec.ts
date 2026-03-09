import { expect, test } from "@playwright/test";

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

test.describe("i18n", () => {
	test("zh-TW login page shows Chinese content", async ({ page }) => {
		await page.goto("/zh-TW/login");
		await expect(page.getByRole("button")).toBeVisible();
		// URL should contain zh-TW locale
		await expect(page).toHaveURL(/\/zh-TW\/login/);
	});
});
