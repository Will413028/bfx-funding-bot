import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@/lib/test-utils";
import type { ExecutionEvent } from "@/types";
import { ExecutionsTable } from "../executions-table";

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

describe("ExecutionsTable", () => {
  it("renders event type badges with amount and APR", () => {
    const { container } = render(
      <ExecutionsTable
        events={EVENTS}
        hasMore={false}
        isFetchingNextPage={false}
        onLoadMore={() => {}}
      />,
    );
    const text = container.textContent ?? "";

    expect(text).toContain("Recent Executions");
    expect(text).toContain("ORDER_FILL");
    expect(text).toContain("RESERVATION_INTENT");
    expect(text).toContain("500");
    // 0.0002 daily → 7.30% APR
    expect(text).toContain("7.30");
  });

  it("calls onLoadMore when the load-more button is clicked", () => {
    const onLoadMore = vi.fn();
    render(
      <ExecutionsTable
        events={EVENTS}
        hasMore={true}
        isFetchingNextPage={false}
        onLoadMore={onLoadMore}
      />,
    );

    fireEvent.click(screen.getByText("Load More"));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("hides load-more and shows empty state without events", () => {
    const { container } = render(
      <ExecutionsTable
        events={[]}
        hasMore={false}
        isFetchingNextPage={false}
        onLoadMore={() => {}}
      />,
    );

    expect(container.textContent).toContain("No execution events yet");
    expect(container.textContent).not.toContain("Load More");
  });
});
