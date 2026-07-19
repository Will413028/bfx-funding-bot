import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import type { OfferClaim } from "@/types";
import { OffersTable } from "../offers-table";

const NOW = Date.now();

const OFFERS: OfferClaim[] = [
  {
    cid: 17123,
    venueOfferId: "3456789",
    state: "claimed",
    symbol: "fUST",
    sizeUsdt: "500.25",
    occurredAtMs: NOW - 10 * 60_000,
    lastUpdatedMs: NOW - 60_000,
  },
  {
    cid: 17124,
    venueOfferId: null,
    state: "pending",
    symbol: "fUST",
    sizeUsdt: "150",
    occurredAtMs: NOW - 30_000,
    lastUpdatedMs: NOW - 30_000,
  },
];

describe("OffersTable", () => {
  it("renders one row per claim with state badge and size", () => {
    const { container } = render(<OffersTable offers={OFFERS} />);
    const text = container.textContent ?? "";

    expect(text).toContain("Active Offers");
    expect(text).toContain("claimed");
    expect(text).toContain("pending");
    expect(text).toContain("500");
    expect(text).toContain("150");
    expect(text).toContain("fUST");
  });

  it("renders empty state without offers", () => {
    const { container } = render(<OffersTable offers={[]} />);
    expect(container.textContent).toContain("No active offers");
  });
});
