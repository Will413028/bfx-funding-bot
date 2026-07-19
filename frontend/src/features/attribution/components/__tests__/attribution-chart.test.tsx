import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import { AttributionChart, toSeries } from "../attribution-chart";

const POINTS = [
  {
    cell: "fUST_p2",
    weekStartMs: 1782691200000,
    weekEndMs: 1783296000000,
    nFills: 2,
    grossInterestUsdt: "0.2",
    netInterestUsdt: "0.17",
    capitalDays: "1000",
    realizedAprNetPct: "6.205",
    baselineCloseAprNetPct: "6.205",
    baselineFrrAprNetPct: null,
    baselineFrrUtilAprNetPct: "8.476",
  },
];

describe("AttributionChart", () => {
  it("renders cell title", () => {
    const { container } = render(
      <AttributionChart cell="fUST_p2" points={POINTS} />,
    );
    expect(container.textContent).toContain("fUST_p2");
  });
});

describe("toSeries", () => {
  it("maps all four APR series, preserving nulls per-line", () => {
    const [row] = toSeries(POINTS);
    expect(row).toMatchObject({
      week: "2026-06-29",
      bot: 6.205,
      close: 6.205,
      frr: null,
      frrUtil: 8.476,
    });
  });

  it("maps a missing util baseline to null", () => {
    const [row] = toSeries([{ ...POINTS[0], baselineFrrUtilAprNetPct: null }]);
    expect(row.frrUtil).toBeNull();
  });
});
