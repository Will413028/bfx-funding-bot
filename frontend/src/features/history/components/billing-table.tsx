"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { formatUSD } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { BillingRecord } from "@/types";

const statusColors: Record<string, string> = {
  paid: "text-emerald-400 border-emerald-400/30",
  pending: "text-amber-500 border-amber-500/30",
  overdue: "text-rose-500 border-rose-500/30",
  waived: "text-zinc-400 border-zinc-400/30",
};

interface BillingTableProps {
  records: BillingRecord[];
}

export function BillingTable({ records }: BillingTableProps) {
  const t = useTranslations("history");

  if (records.length === 0) {
    return (
      <p className="py-12 text-center text-sm text-zinc-500">
        {t("noBilling")}
      </p>
    );
  }

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      {/* Header */}
      <div className="grid grid-cols-[1fr_80px_1fr_80px_1fr] gap-2 border-b border-white/5 px-4 py-2.5 text-xs font-medium uppercase tracking-wider text-zinc-500">
        <span>{t("period")}</span>
        <span>{t("plan")}</span>
        <span className="text-right">{t("amount")}</span>
        <span>{t("status")}</span>
        <span className="text-right">{t("paid")}</span>
      </div>
      {/* Rows */}
      {records.map((r) => (
        <div
          key={r.id}
          className="grid grid-cols-[1fr_80px_1fr_80px_1fr] items-center gap-2 border-b border-white/[0.03] px-4 py-2.5 text-sm last:border-0"
        >
          <span className="text-zinc-400">
            {new Date(r.periodStart).toLocaleDateString()} –{" "}
            {new Date(r.periodEnd).toLocaleDateString()}
          </span>
          <Badge variant="secondary">{r.plan}</Badge>
          <span className="text-right tabular-nums text-foreground">
            {formatUSD(r.amount)}
          </span>
          <Badge variant="outline" className={cn(statusColors[r.status] ?? "")}>
            {r.status}
          </Badge>
          <span className="text-right text-xs text-zinc-500">
            {r.paidAt ? new Date(r.paidAt).toLocaleDateString() : "—"}
          </span>
        </div>
      ))}
    </div>
  );
}
