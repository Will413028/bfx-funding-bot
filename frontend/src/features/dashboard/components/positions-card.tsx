"use client";

import { useLocale, useTranslations } from "next-intl";
import { DecimalAmount } from "@/components/shared/decimal-amount";
import { formatRelativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Position } from "@/types";

export type ReconcileFreshness = "fresh" | "lagging" | "stale" | "never";

const FRESH_MS = 5 * 60_000;
const LAGGING_MS = 30 * 60_000;

/** Bucket reconcile age: ≤5m fresh, ≤30m lagging, older stale. */
export function reconcileFreshness(
  lastReconciledAtMs: number | null,
  nowMs = Date.now(),
): ReconcileFreshness {
  if (lastReconciledAtMs == null) return "never";
  const age = nowMs - lastReconciledAtMs;
  if (age <= FRESH_MS) return "fresh";
  if (age <= LAGGING_MS) return "lagging";
  return "stale";
}

const freshnessDot: Record<ReconcileFreshness, string> = {
  fresh: "bg-emerald-400 animate-pulse",
  lagging: "bg-amber-500",
  stale: "bg-rose-500",
  never: "bg-zinc-600",
};

interface PositionsCardProps {
  positions: Position[];
}

export function PositionsCard({ positions }: PositionsCardProps) {
  const t = useTranslations("overview");
  const locale = useLocale();

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("positions")}
      </h3>

      {positions.length === 0 ? (
        <p className="mt-4 text-sm text-zinc-500">{t("noPositions")}</p>
      ) : (
        <ul className="mt-4 space-y-5">
          {positions.map((p) => {
            const freshness = reconcileFreshness(p.lastReconciledAtMs);
            return (
              <li key={p.symbol} className="space-y-2">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-sm text-zinc-200">
                    {p.symbol}
                  </span>
                  <span
                    className="flex items-center gap-1.5 text-xs text-zinc-500"
                    title={
                      p.lastReconciledAtMs == null
                        ? undefined
                        : new Date(p.lastReconciledAtMs).toISOString()
                    }
                  >
                    <span
                      className={cn(
                        "size-1.5 rounded-full",
                        freshnessDot[freshness],
                      )}
                    />
                    {p.lastReconciledAtMs == null
                      ? t("reconcileNever")
                      : `${t("reconciled")} ${formatRelativeTime(p.lastReconciledAtMs, locale)}`}
                  </span>
                </div>
                <dl className="grid grid-cols-3 gap-2">
                  <div>
                    <dt className="text-xs text-zinc-500">{t("reserved")}</dt>
                    <dd className="mt-0.5 font-medium">
                      <DecimalAmount
                        value={p.reserved}
                        className={
                          Number(p.reserved) > 0
                            ? "text-amber-500"
                            : "text-zinc-400"
                        }
                      />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-zinc-500">{t("realized")}</dt>
                    <dd className="mt-0.5 font-medium">
                      <DecimalAmount
                        value={p.realized}
                        className="text-emerald-400"
                      />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-zinc-500">{t("credits")}</dt>
                    <dd className="mt-0.5 font-medium tabular-nums tracking-tight text-zinc-200">
                      {p.nCredits ?? "—"}
                    </dd>
                  </div>
                </dl>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
