import { expect, test } from "@playwright/test";

const accountId =
	process.env.E2E_EXCHANGE_ACCOUNT_ID ??
	"550e8400-e29b-41d4-a716-446655440000";

test.describe("symbol-scoped uncertainty console", () => {
	test.skip(
		!process.env.E2E_FULL_STACK,
		"requires authenticated full stack — see e2e/README.md",
	);

	test("shows the blocked symbol without offering a submit retry", async ({
		page,
	}) => {
		await page.route(
			`**/api/proxy/exchange-accounts/${accountId}/positions**`,
			async (route) => {
				await route.fulfill({
					status: 200,
					contentType: "application/json",
					body: JSON.stringify({
						data: [
							{
								symbol: "fUST",
								available: "50",
								offered: "100",
								lent: "0",
								unattributedLent: null,
								nCredits: 0,
								lastUpdatedMs: Date.now(),
								lastReconciledAtMs: Date.now(),
							},
							{
								symbol: "fUSD",
								available: "5",
								offered: "25",
								lent: "1",
								unattributedLent: "1",
								nCredits: 1,
								lastUpdatedMs: Date.now(),
								lastReconciledAtMs: Date.now(),
							},
						],
					}),
				});
			},
		);
		await page.route(
			`**/api/proxy/exchange-accounts/${accountId}/uncertainties**`,
			async (route) => {
				await route.fulfill({
					status: 200,
					contentType: "application/json",
					body: JSON.stringify({
						data: [
							{
								uncertaintyId: "u-e2e",
								kind: "submit_outcome_unknown",
								symbol: "fUST",
								intendedAmount: "100",
								state: "open",
								evidenceSummary: { observedAtMs: Date.now() },
								resolutionContext: {
									evidenceRef: "12",
									queryStartedAtMs: Date.now() - 1000,
									queryFinishedAtMs: Date.now(),
									candidateCount: 0,
									candidateVenueOfferIds: [],
									unavailableReason: null,
								},
								blockedScope: {
									exchangeAccountId: accountId,
									environment: "ci",
									symbol: "fUST",
								},
							},
						],
					}),
				});
			},
		);
		await page.goto("/en/overview");
		const banner = page.getByRole("region", {
			name: "Trading is blocked for an uncertain symbol",
		});
		await expect(banner.getByText("fUST", { exact: true })).toBeVisible();
		await expect(banner.getByText(/fUSD.*clear/)).toBeVisible();
		const positions = page.getByRole("heading", { name: "Positions" }).locator("..");
		await expect(positions.getByText("fUSD", { exact: true })).toBeVisible();
		await expect(
			banner.getByRole("button", { name: /retry|resubmit/i }),
		).toHaveCount(0);
	});
});
