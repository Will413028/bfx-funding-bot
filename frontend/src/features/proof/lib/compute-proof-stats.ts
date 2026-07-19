import type { PublicProofWeek } from "@/types";

export interface ProofStats {
  /** Mean realized APR (%) over the trailing window; null if no week in the
   * window had any fills (all realizedAprNetPct null). */
  avgRealizedAprPct: number | null;
  /** Mean util-adjusted-FRR baseline APR (%) over the trailing window. */
  avgBaselineAprPct: number | null;
  /** avgRealizedAprPct - avgBaselineAprPct; null unless both sides exist. */
  spreadPct: number | null;
  /** Total weeks in the series (not just the trailing window). */
  weeksTracked: number;
}

const DEFAULT_WINDOW_WEEKS = 12;

function mean(values: number[]): number | null {
  if (values.length === 0) return null;
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

/** Blend the trailing `windowWeeks` of the public series into report-card
 * stats. Pure — no fetching, no formatting (see format.ts for that). */
export function computeProofStats(
  weeks: PublicProofWeek[],
  windowWeeks: number = DEFAULT_WINDOW_WEEKS,
): ProofStats {
  const recent = weeks.slice(-windowWeeks);
  const realized = recent
    .map((w) =>
      w.realizedAprNetPct === null ? null : Number(w.realizedAprNetPct),
    )
    .filter((v): v is number => v !== null);
  const baseline = recent
    .map((w) =>
      w.baselineFrrUtilAprNetPct === null
        ? null
        : Number(w.baselineFrrUtilAprNetPct),
    )
    .filter((v): v is number => v !== null);

  const avgRealizedAprPct = mean(realized);
  const avgBaselineAprPct = mean(baseline);
  const spreadPct =
    avgRealizedAprPct !== null && avgBaselineAprPct !== null
      ? avgRealizedAprPct - avgBaselineAprPct
      : null;

  return {
    avgRealizedAprPct,
    avgBaselineAprPct,
    spreadPct,
    weeksTracked: weeks.length,
  };
}
