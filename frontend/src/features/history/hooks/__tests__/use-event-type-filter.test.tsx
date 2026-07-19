import { act, renderHook } from "@testing-library/react";
import { withNuqsTestingAdapter } from "nuqs/adapters/testing";
import { describe, expect, it, vi } from "vitest";
import { useEventTypeFilter } from "../use-event-type-filter";

describe("useEventTypeFilter", () => {
  it("defaults to null (all events)", () => {
    const { result } = renderHook(() => useEventTypeFilter(), {
      wrapper: withNuqsTestingAdapter(),
    });
    expect(result.current[0]).toBeNull();
  });

  it("reads a valid eventType from the URL", () => {
    const { result } = renderHook(() => useEventTypeFilter(), {
      wrapper: withNuqsTestingAdapter({
        searchParams: "?eventType=ORDER_FILL",
      }),
    });
    expect(result.current[0]).toBe("ORDER_FILL");
  });

  it("rejects unknown eventType values as null", () => {
    const { result } = renderHook(() => useEventTypeFilter(), {
      wrapper: withNuqsTestingAdapter({
        searchParams: "?eventType=NOT_A_TYPE",
      }),
    });
    expect(result.current[0]).toBeNull();
  });

  it("writes the selected type to the URL", async () => {
    const onUrlUpdate = vi.fn();
    const { result } = renderHook(() => useEventTypeFilter(), {
      wrapper: withNuqsTestingAdapter({ onUrlUpdate }),
    });

    await act(() => result.current[1]("CREDIT_CLOSED"));

    expect(result.current[0]).toBe("CREDIT_CLOSED");
    expect(onUrlUpdate.mock.lastCall?.[0].searchParams.get("eventType")).toBe(
      "CREDIT_CLOSED",
    );
  });
});
