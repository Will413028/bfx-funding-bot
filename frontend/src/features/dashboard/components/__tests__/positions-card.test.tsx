import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import type { Position } from "@/types";
import { PositionsCard, reconcileFreshness } from "../positions-card";

const NOW = 1_790_000_000_000;

const POSITION: Position = {
  symbol: "fUST",
  reserved: "1250.5",
  realized: "37.1234",
  nCredits: 4,
  lastUpdatedMs: NOW - 60_000,
  lastReconciledAt: NOW - 120_000,
  lastEventSeq: 981,
};

describe("PositionsCard", () => {
  it("renders symbol, formatted amounts and credits count", () => {
    const { container } = render(<PositionsCard positions={[POSITION]} />);
    const text = container.textContent ?? "";

    expect(text).toContain("fUST");
    expect(text).toContain("1,250");
    expect(text).toContain("37");
    expect(text).toContain("4");
    expect(text).toContain("Reserved");
    expect(text).toContain("Realized");
  });

  it("shows never-reconciled label when lastReconciledAt is null", () => {
    const { container } = render(
      <PositionsCard positions={[{ ...POSITION, lastReconciledAt: null }]} />,
    );
    expect(container.textContent).toContain("Never reconciled");
  });

  it("renders empty state without positions", () => {
    const { container } = render(<PositionsCard positions={[]} />);
    expect(container.textContent).toContain("No positions yet");
  });
});

describe("reconcileFreshness", () => {
  it("buckets by reconcile age", () => {
    expect(reconcileFreshness(NOW - 60_000, NOW)).toBe("fresh");
    expect(reconcileFreshness(NOW - 5 * 60_000, NOW)).toBe("fresh");
    expect(reconcileFreshness(NOW - 6 * 60_000, NOW)).toBe("lagging");
    expect(reconcileFreshness(NOW - 31 * 60_000, NOW)).toBe("stale");
  });

  it("maps null to never", () => {
    expect(reconcileFreshness(null, NOW)).toBe("never");
  });
});
