import { describe, expect, it } from "vitest";
import { cn } from "../utils";

describe("cn", () => {
  it("merges classes", () => {
    expect(cn("px-2", "py-1")).toBe("px-2 py-1");
  });

  it("tailwind-merge resolves conflicts", () => {
    expect(cn("px-2 py-1", "px-4")).toBe("py-1 px-4");
  });

  it("handles falsy values", () => {
    expect(cn("base", false && "hidden", "text-sm")).toBe("base text-sm");
  });

  it("handles undefined and null", () => {
    expect(cn("base", undefined, null, "text-sm")).toBe("base text-sm");
  });

  it("handles empty string", () => {
    expect(cn("")).toBe("");
  });
});
