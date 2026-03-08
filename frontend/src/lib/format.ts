/** Annual Percentage Rate from daily rate (e.g. 0.0001 → "3.65%") */
export function formatAPR(dailyRate: number): string {
  return `${(dailyRate * 365 * 100).toFixed(2)}%`;
}

/** Daily rate as percentage (e.g. 0.0001 → "0.0100%") */
export function formatDailyRate(rate: number): string {
  return `${(rate * 100).toFixed(4)}%`;
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
