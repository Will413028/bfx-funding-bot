import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import type { DashboardSummary, EarningsSummary } from "@/types";
import { StatsGrid } from "../stats-grid";

const mockDashboard: DashboardSummary = {
  wallet: { currency: "USD", balance: 8000, balanceAvailable: 5000 },
  offers: [],
  credits: [],
  market: null,
  engineReady: true,
};

const mockEarnings: EarningsSummary = {
  estimatedDailyEarning: 12.5,
  weightedAPY: 0.0365,
  earnings7d: 87.5,
  earnings30d: 375,
  totalLent: 50000,
  activeCredits: 3,
  currency: "USD",
};

describe("StatsGrid", () => {
  it("renders 4 stat card labels", () => {
    const { container } = render(
      <StatsGrid dashboard={mockDashboard} earnings={mockEarnings} />,
    );
    const text = container.textContent ?? "";

    expect(text).toContain("Total Lent");
    expect(text).toContain("Available Balance");
    expect(text).toContain("Est. Daily Earning");
    expect(text).toContain("Worker Status");
  });

  it("shows Running when engine is ready", () => {
    const { container } = render(
      <StatsGrid dashboard={mockDashboard} earnings={mockEarnings} />,
    );
    expect(container.textContent).toContain("Running");
  });

  it("shows Stopped when engine not ready", () => {
    const { container } = render(
      <StatsGrid
        dashboard={{ ...mockDashboard, engineReady: false }}
        earnings={mockEarnings}
      />,
    );
    expect(container.textContent).toContain("Stopped");
  });

  it("displays active credits count", () => {
    const { container } = render(
      <StatsGrid dashboard={mockDashboard} earnings={mockEarnings} />,
    );
    expect(container.textContent).toContain("3 active credits");
  });

  it("displays APY subtitle", () => {
    const { container } = render(
      <StatsGrid dashboard={mockDashboard} earnings={mockEarnings} />,
    );
    expect(container.textContent).toContain("APY 3.65%");
  });

  it("handles null wallet gracefully", () => {
    const { container } = render(
      <StatsGrid
        dashboard={{ ...mockDashboard, wallet: null }}
        earnings={mockEarnings}
      />,
    );
    expect(container.textContent).toContain("Available Balance");
  });
});
