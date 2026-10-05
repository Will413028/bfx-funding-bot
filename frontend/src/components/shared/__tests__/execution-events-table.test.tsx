import { describe, expect, it } from "vitest";
import { render } from "@/lib/test-utils";
import type { ExecutionEvent } from "@/types";
import { ExecutionEventsTable } from "../execution-events-table";

const NOW = Date.now();

const EVENTS: ExecutionEvent[] = [
  {
    eventKey: "981",
    eventType: "ORDER_FILL",
    occurredAtMs: NOW - 120_000,
    symbol: "fUST",
    venueOfferId: "3456789",
    cid: 17123,
    amount: "500.25",
    rate: 0.0002,
  },
  {
    eventKey: "980",
    eventType: "RESERVATION_INTENT",
    occurredAtMs: NOW - 180_000,
    symbol: "fUST",
    venueOfferId: null,
    cid: 17123,
    amount: null,
    rate: null,
  },
];

describe("ExecutionEventsTable", () => {
  it("renders event type badges with amount and APR", () => {
    const { container } = render(<ExecutionEventsTable events={EVENTS} />);
    const text = container.textContent ?? "";

    expect(text).toContain("ORDER_FILL");
    expect(text).toContain("RESERVATION_INTENT");
    expect(text).toContain("500");
    // 0.0002 daily → 7.30% APR
    expect(text).toContain("7.30");
  });

  it("renders the empty state without events", () => {
    const { container } = render(<ExecutionEventsTable events={[]} />);
    expect(container.textContent).toContain("No execution events yet");
  });
});
