import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import { ProofReportCard } from "../proof-report-card";

describe("ProofReportCard", () => {
  it("renders formatted stats with a sign on the spread", () => {
    const { container } = render(
      <ProofReportCard
        stats={{
          avgRealizedAprPct: 6.205,
          avgBaselineAprPct: 4.1,
          spreadPct: 2.105,
          weeksTracked: 8,
        }}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("6.21%");
    // 6.205 - 4.1 = 2.105, which toFixed(2) rounds to "2.10" under IEEE 754
    // binary float representation — asserting the exact computed string here
    // (not a rounded-up "2.11") guards against a future formatPercent change
    // silently altering rounding behavior.
    expect(text).toContain("+2.10%");
    expect(text).toContain("8");
  });

  it("renders a negative spread without a doubled minus sign", () => {
    const { container } = render(
      <ProofReportCard
        stats={{
          avgRealizedAprPct: 3.0,
          avgBaselineAprPct: 4.5,
          spreadPct: -1.5,
          weeksTracked: 4,
        }}
      />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("-1.50%");
    expect(text).not.toContain("+-1.50%");
  });

  it("renders an em dash placeholder when there is no data yet", () => {
    const { container } = render(
      <ProofReportCard
        stats={{
          avgRealizedAprPct: null,
          avgBaselineAprPct: null,
          spreadPct: null,
          weeksTracked: 0,
        }}
      />,
    );
    const text = container.textContent ?? "";
    expect(text.match(/—/g)?.length).toBe(2);
    expect(text).toContain("0");
  });
});
