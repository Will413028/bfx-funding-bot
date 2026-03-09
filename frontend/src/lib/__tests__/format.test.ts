import { describe, expect, it } from "vitest";
import { formatAPR, formatDailyRate, formatPeriod, formatUSD } from "../format";

describe("formatAPR", () => {
  it("converts daily rate to annual percentage", () => {
    expect(formatAPR(0.0001)).toBe("3.65%");
  });

  it("handles zero", () => {
    expect(formatAPR(0)).toBe("0.00%");
  });

  it("handles higher rates", () => {
    expect(formatAPR(0.001)).toBe("36.50%");
  });
});

describe("formatDailyRate", () => {
  it("formats daily rate as percentage", () => {
    expect(formatDailyRate(0.0001)).toBe("0.0100%");
  });

  it("handles zero", () => {
    expect(formatDailyRate(0)).toBe("0.0000%");
  });
});

describe("formatUSD", () => {
  it("formats with dollar sign and commas", () => {
    expect(formatUSD(50000)).toBe("$50,000.00");
  });

  it("formats small amounts", () => {
    expect(formatUSD(1234.5)).toBe("$1,234.50");
  });

  it("handles zero", () => {
    expect(formatUSD(0)).toBe("$0.00");
  });

  it("handles negative amounts", () => {
    expect(formatUSD(-100)).toBe("-$100.00");
  });
});

describe("formatPeriod", () => {
  it("appends d suffix", () => {
    expect(formatPeriod(30)).toBe("30d");
  });

  it("handles single digit", () => {
    expect(formatPeriod(2)).toBe("2d");
  });
});
