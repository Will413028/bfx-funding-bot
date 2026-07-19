/** Annual Percentage Rate from daily rate (e.g. 0.0001 → "3.65%") */
export function formatAPR(dailyRate: number): string {
  return `${(dailyRate * 365 * 100).toFixed(2)}%`;
}

/** Daily rate as percentage (e.g. 0.0001 → "0.0100%") */
export function formatDailyRate(rate: number): string {
  return `${(rate * 100).toFixed(4)}%`;
}

/** An already-annualized percentage value (e.g. 6.205 → "6.21%"). Unlike
 * formatAPR, this does NOT multiply by 365 — use for APR/return figures the
 * backend already annualized (attribution + public proof-page series). */
export function formatPercent(value: number, fractionDigits = 2): string {
  return `${value.toFixed(fractionDigits)}%`;
}

/** USD currency format (e.g. 50000 → "$50,000.00") */
export function formatUSD(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
  }).format(amount);
}

/** Lending period in days (e.g. 30 → "30d") */
export function formatPeriod(days: number): string {
  return `${days}d`;
}

const RELATIVE_UNITS: ReadonlyArray<[Intl.RelativeTimeFormatUnit, number]> = [
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
];

/** Localized relative time from epoch ms (e.g. "5 min. ago" / "5 分鐘前") */
export function formatRelativeTime(
  epochMs: number,
  locale = "en",
  nowMs = Date.now(),
): string {
  const delta = epochMs - nowMs;
  const rtf = new Intl.RelativeTimeFormat(locale, {
    numeric: "always",
    style: "narrow",
  });
  for (const [unit, msPerUnit] of RELATIVE_UNITS) {
    if (Math.abs(delta) >= msPerUnit) {
      return rtf.format(Math.trunc(delta / msPerUnit), unit);
    }
  }
  return rtf.format(Math.trunc(delta / 1000), "second");
}
