import { cn } from "@/lib/utils";

interface DecimalAmountProps {
  /** Decimal string from the API (e.g. "12345.678") or a number. */
  value: string | number;
  maxFractionDigits?: number;
  className?: string;
}

/**
 * Tabular monetary value with de-emphasized fraction digits
 * (financial-typography: "5,000" bright, ".00" dimmed).
 */
export function DecimalAmount({
  value,
  maxFractionDigits = 2,
  className,
}: DecimalAmountProps) {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) {
    return <span className={className}>—</span>;
  }
  const formatted = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: Math.max(2, maxFractionDigits),
  }).format(n);
  const dot = formatted.lastIndexOf(".");
  return (
    <span className={cn("tabular-nums tracking-tight", className)}>
      {dot === -1 ? (
        formatted
      ) : (
        <>
          {formatted.slice(0, dot)}
          <span className="text-zinc-500">{formatted.slice(dot)}</span>
        </>
      )}
    </span>
  );
}
