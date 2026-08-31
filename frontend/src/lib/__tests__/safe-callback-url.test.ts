import { describe, expect, it } from "vitest";
import { safeCallbackUrl } from "../safe-callback-url";

describe("safeCallbackUrl", () => {
  it.each([
    ["/overview", "/overview"],
    ["/history?cursor=next", "/history?cursor=next"],
  ])("preserves internal callback %s", (value, expected) => {
    expect(safeCallbackUrl(value)).toBe(expected);
  });

  it.each(["https://evil.example", "//evil.example", "/\\evil.example", ""])(
    "falls back for external or malformed callback %s",
    (value) => {
      expect(safeCallbackUrl(value)).toBe("/overview");
    },
  );
});
