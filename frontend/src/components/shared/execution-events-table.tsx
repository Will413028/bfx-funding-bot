"use client";

import { useLocale, useTranslations } from "next-intl";
import { DecimalAmount } from "@/components/shared/decimal-amount";
import { Badge } from "@/components/ui/badge";
import { formatRelativeTime } from "@/lib/format";
import type { ExecutionEvent } from "@/types";

// Event lifecycle colors: intent = amber (queued), claimed = cyan (live),
// fill = emerald (yielding), released = zinc, failed = rose,
// credit closed = violet (bot compounding cycle complete).
const eventStyles: Record<string, string> = {
  RESERVATION_INTENT: "text-amber-500 border-amber-500/30",
  RESERVATION_CLAIMED: "text-cyan-400 border-cyan-400/30",
  ORDER_FILL: "text-emerald-400 border-emerald-400/30",
  RESERVATION_RELEASED: "text-zinc-400 border-zinc-400/30",
  RESERVATION_FAILED: "text-rose-500 border-rose-500/30",
  CREDIT_CLOSED: "text-violet-400 border-violet-400/30",
};

/** APR as the hero number, % sign de-emphasized (financial typography). */
function AprValue({ rate }: { rate: number }) {
  return (
    <span className="tabular-nums tracking-tight text-emerald-400">
      {(rate * 365 * 100).toFixed(2)}
      <span className="text-xs text-zinc-500">%</span>
    </span>
  );
}

interface ExecutionEventsTableProps {
  events: ExecutionEvent[];
}

/**
 * Pure event_log table (headers + rows + empty state) shared between the
 * dashboard "Recent Executions" card and the full-page executions browser.
 * Callers own the surrounding card chrome and load-more control.
 */
export function ExecutionEventsTable({ events }: ExecutionEventsTableProps) {
  const t = useTranslations("executions");
  const locale = useLocale();

  if (events.length === 0) {
    return <p className="text-sm text-zinc-500">{t("noEvents")}</p>;
  }

  return (
    <div className="overflow-x-auto">
      <div className="min-w-[560px]">
        <div className="grid grid-cols-[170px_70px_1fr_1fr_1fr] gap-2 border-b border-white/5 pb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">
          <span>{t("event")}</span>
          <span>{t("symbol")}</span>
          <span className="text-right">{t("amount")}</span>
          <span className="text-right">{t("rateApr")}</span>
          <span className="text-right">{t("time")}</span>
        </div>
        {events.map((e) => (
          <div
            key={e.eventSeq}
            className="grid grid-cols-[170px_70px_1fr_1fr_1fr] items-center gap-2 border-b border-white/[0.03] py-2.5 text-sm last:border-0"
          >
            <Badge
              variant="outline"
              className={eventStyles[e.eventType] ?? "text-zinc-400"}
            >
              {e.eventType}
            </Badge>
            <span className="text-zinc-400">{e.symbol ?? "—"}</span>
            <span className="text-right font-medium text-foreground">
              {e.amount == null ? (
                <span className="text-zinc-600">—</span>
              ) : (
                <DecimalAmount value={e.amount} />
              )}
            </span>
            <span className="text-right">
              {e.rate == null ? (
                <span className="text-zinc-600">—</span>
              ) : (
                <AprValue rate={e.rate} />
              )}
            </span>
            <span
              className="text-right text-xs text-zinc-500"
              title={new Date(e.occurredAtMs).toISOString()}
            >
              {formatRelativeTime(e.occurredAtMs, locale)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
