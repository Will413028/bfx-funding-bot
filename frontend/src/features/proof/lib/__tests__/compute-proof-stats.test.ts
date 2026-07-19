import { describe, expect, it } from "vitest";
import type { PublicProofWeek } from "@/types";
import { computeProofStats } from "../compute-proof-stats";

function week(
  weekStartMs: number,
  realizedAprNetPct: string | null,
  baselineFrrUtilAprNetPct: string | null,
): PublicProofWeek {
  return { weekStartMs, realizedAprNetPct, baselineFrrUtilAprNetPct };
}

describe("computeProofStats", () => {
  it("averages realized/baseline and computes the spread", () => {
    const weeks = [week(1, "4.0", "3.0"), week(2, "6.0", "5.0")];
    const stats = computeProofStats(weeks);
    expect(stats.avgRealizedAprPct).toBe(5);
    expect(stats.avgBaselineAprPct).toBe(4);
    expect(stats.spreadPct).toBe(1);
    expect(stats.weeksTracked).toBe(2);
  });

  it("ignores null weeks (no fills that week) when averaging realized", () => {
    const weeks = [week(1, null, "3.0"), week(2, "6.0", "5.0")];
    const stats = computeProofStats(weeks);
    expect(stats.avgRealizedAprPct).toBe(6);
    expect(stats.avgBaselineAprPct).toBe(4);
    // both sides still resolve, so a spread exists
    expect(stats.spreadPct).toBe(2);
  });

  it("returns nulls (not NaN/crash) when nothing in the window has data", () => {
    const weeks = [week(1, null, null)];
    const stats = computeProofStats(weeks);
    expect(stats.avgRealizedAprPct).toBeNull();
    expect(stats.avgBaselineAprPct).toBeNull();
    expect(stats.spreadPct).toBeNull();
    expect(stats.weeksTracked).toBe(1);
  });

  it("only blends the trailing windowWeeks, but weeksTracked stays total", () => {
    const weeks = [
      week(1, "100.0", "0.0"), // outside a 2-week window — must not skew the average
      week(2, "4.0", "3.0"),
      week(3, "6.0", "5.0"),
    ];
    const stats = computeProofStats(weeks, 2);
    expect(stats.avgRealizedAprPct).toBe(5);
    expect(stats.weeksTracked).toBe(3);
  });

  it("returns an empty-series shape without dividing by zero", () => {
    const stats = computeProofStats([]);
    expect(stats).toEqual({
      avgRealizedAprPct: null,
      avgBaselineAprPct: null,
      spreadPct: null,
      weeksTracked: 0,
    });
  });
});
