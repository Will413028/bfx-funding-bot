import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@/lib/test-utils";
import { EventTypeFilter } from "../event-type-filter";

afterEach(cleanup);

describe("EventTypeFilter", () => {
  it("renders the All chip plus all six event types", () => {
    const { getByText } = render(
      <EventTypeFilter value={null} onChange={() => {}} />,
    );

    expect(getByText("All")).toBeDefined();
    for (const type of [
      "RESERVATION_INTENT",
      "RESERVATION_CLAIMED",
      "RESERVATION_FAILED",
      "ORDER_FILL",
      "RESERVATION_RELEASED",
      "CREDIT_CLOSED",
    ]) {
      expect(getByText(type)).toBeDefined();
    }
  });

  it("marks the active chip with aria-pressed", () => {
    const { getByText } = render(
      <EventTypeFilter value="ORDER_FILL" onChange={() => {}} />,
    );

    expect(getByText("ORDER_FILL").getAttribute("aria-pressed")).toBe("true");
    expect(getByText("All").getAttribute("aria-pressed")).toBe("false");
  });

  it("emits the type on chip click and null on All", () => {
    const onChange = vi.fn();
    const { getByText } = render(
      <EventTypeFilter value="ORDER_FILL" onChange={onChange} />,
    );

    fireEvent.click(getByText("CREDIT_CLOSED"));
    expect(onChange).toHaveBeenLastCalledWith("CREDIT_CLOSED");

    fireEvent.click(getByText("All"));
    expect(onChange).toHaveBeenLastCalledWith(null);
  });
});
