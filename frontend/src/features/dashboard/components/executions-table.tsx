"use client";

import { useLocale, useTranslations } from "next-intl";
import { DecimalAmount } from "@/components/shared/decimal-amount";
import { LoadMoreButton } from "@/components/shared/load-more-button";
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

interface ExecutionsTableProps {
  events: ExecutionEvent[];
  hasMore: boolean;
  isFetchingNextPage: boolean;
  onLoadMore: () => void;
}

export function ExecutionsTable({
  events,
  hasMore,
  isFetchingNextPage,
  onLoadMore,
}: ExecutionsTableProps) {
  const t = useTranslations("overview");
  const locale = useLocale();

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("recentExecutions")}
      </h3>

      {events.length === 0 ? (
        <p className="mt-4 text-sm text-zinc-500">{t("noExecutions")}</p>
      ) : (
        <div className="mt-4 overflow-x-auto">
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
      )}

      <LoadMoreButton
        hasMore={hasMore}
        isFetchingNextPage={isFetchingNextPage}
        onClick={onLoadMore}
      />
    </div>
  );
}
