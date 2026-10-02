import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import type { Position } from "@/types";
import { PositionsCard, reconcileFreshness } from "../positions-card";

const NOW = 1_790_000_000_000;

const POSITION: Position = {
  symbol: "fUST",
  available: "9000.5",
  offered: "1250.5",
  lent: "37.1234",
  unattributedLent: null,
  nCredits: 4,
  lastUpdatedMs: NOW - 60_000,
  lastReconciledAtMs: NOW - 120_000,
};

describe("PositionsCard", () => {
  it("renders symbol, formatted amounts and credits count", () => {
    const { container } = render(<PositionsCard positions={[POSITION]} />);
    const text = container.textContent ?? "";

    expect(text).toContain("fUST");
    expect(text).toContain("1,250");
    expect(text).toContain("37");
    expect(text).toContain("4");
    expect(text).toContain("9,000");
    expect(text).toContain("Available");
    expect(text).toContain("Offered");
    expect(text).toContain("Lent");
    expect(text).not.toContain("Reserved");
    expect(text).not.toContain("Realized");
  });

  it("hides unattributed lent when null or zero", () => {
    for (const unattributedLent of [null, "0"]) {
      const { container, unmount } = render(
        <PositionsCard positions={[{ ...POSITION, unattributedLent }]} />,
      );
      expect(container.textContent).not.toContain("Unattributed lent");
      unmount();
    }
  });

  it("shows unattributed lent under Lent, never added into a figure", () => {
    const { container } = render(
      <PositionsCard positions={[{ ...POSITION, unattributedLent: "12.5" }]} />,
    );
    const sub = container.querySelector('[data-testid="unattributed-lent"]');
    expect(sub?.textContent).toContain("Unattributed lent");
    expect(sub?.textContent).toContain("12.50");
    // Lent is rendered as-is (37.12), not 37.12 + 12.5.
    const text = container.textContent ?? "";
    expect(text).toContain("37.12");
    expect(text).not.toContain("49.62");
  });

  it("shows never-reconciled label when lastReconciledAtMs is null", () => {
    const { container } = render(
      <PositionsCard positions={[{ ...POSITION, lastReconciledAtMs: null }]} />,
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
