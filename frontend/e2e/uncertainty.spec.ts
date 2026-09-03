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
								openedEventSeq: 7,
								reconcileEventSeq: null,
								resolvedEventSeq: null,
								evidenceSummary: { observedAtMs: Date.now() },
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
		await expect(page.getByText("fUST").first()).toBeVisible();
		await expect(
			page.getByRole("button", { name: /retry|resubmit/i }),
		).toHaveCount(0);
	});
});
