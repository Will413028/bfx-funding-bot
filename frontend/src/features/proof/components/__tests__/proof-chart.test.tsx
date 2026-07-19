import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import { ProofChart, toSeries } from "../proof-chart";

const WEEKS = [
  {
    weekStartMs: 1782691200000,
    realizedAprNetPct: "10950",
    baselineFrrUtilAprNetPct: "5",
  },
  {
    weekStartMs: 1783296000000,
    realizedAprNetPct: null,
    baselineFrrUtilAprNetPct: "3",
  },
];

describe("toSeries", () => {
  it("maps both series, preserving nulls per-line", () => {
    const rows = toSeries(WEEKS);
    expect(rows[0]).toMatchObject({
      week: "2026-06-29",
      bot: 10950,
      baseline: 5,
    });
    expect(rows[1]).toMatchObject({
      week: "2026-07-06",
      bot: null,
      baseline: 3,
    });
  });
});

describe("ProofChart", () => {
  it("renders without crashing on a populated series", () => {
    const { container } = render(<ProofChart weeks={WEEKS} />);
    expect(
      container.querySelector(".recharts-responsive-container"),
    ).toBeTruthy();
  });

  it("renders without crashing on an empty series", () => {
    const { container } = render(<ProofChart weeks={[]} />);
    expect(
      container.querySelector(".recharts-responsive-container"),
    ).toBeTruthy();
  });
});
