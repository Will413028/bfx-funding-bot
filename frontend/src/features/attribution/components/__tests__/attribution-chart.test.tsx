import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import { AttributionChart } from "../attribution-chart";

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
