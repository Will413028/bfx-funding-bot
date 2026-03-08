"use client";

import { Badge } from "@/components/ui/badge";
import { formatAPR, formatPeriod, formatUSD } from "@/lib/format";
import type { ExecutionRecord } from "@/types";

const actionColors: Record<string, string> = {
  place: "text-emerald-400 border-emerald-400/30",
  cancel: "text-amber-500 border-amber-500/30",
  filled: "text-sky-400 border-sky-400/30",
  renew: "text-violet-400 border-violet-400/30",
};

interface ExecutionTableProps {
  records: ExecutionRecord[];
}

export function ExecutionTable({ records }: ExecutionTableProps) {
  if (records.length === 0) {
    return (
      <p className="py-12 text-center text-sm text-zinc-500">
        No execution records
      </p>
    );
  }

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      {/* Header */}
      <div className="grid grid-cols-[80px_60px_1fr_1fr_60px_80px_1fr] gap-2 border-b border-white/5 px-4 py-2.5 text-xs font-medium uppercase tracking-wider text-zinc-500">
        <span>Action</span>
        <span>CCY</span>
        <span className="text-right">Amount</span>
        <span className="text-right">Rate (APR)</span>
        <span className="text-right">Period</span>
        <span>Status</span>
        <span className="text-right">Time</span>
      </div>
      {/* Rows */}
      {records.map((r) => (
        <div
          key={r.id}
          className="grid grid-cols-[80px_60px_1fr_1fr_60px_80px_1fr] items-center gap-2 border-b border-white/[0.03] px-4 py-2.5 text-sm last:border-0"
        >
          <Badge variant="outline" className={actionColors[r.action] ?? ""}>
            {r.action}
          </Badge>
          <span className="text-zinc-400">{r.currency}</span>
          <span className="text-right tabular-nums text-foreground">
            {formatUSD(r.amount)}
          </span>
          <span className="text-right tabular-nums text-emerald-400">
            {formatAPR(r.rate)}
          </span>
          <span className="text-right tabular-nums text-zinc-400">
            {formatPeriod(r.period)}
          </span>
          <span className="text-xs text-zinc-400">{r.status}</span>
          <span className="text-right text-xs text-zinc-500">
            {new Date(r.createdAt).toLocaleDateString()}
          </span>
        </div>
      ))}
    </div>
  );
}
