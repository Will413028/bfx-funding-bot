"use client";

import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";
import type { ExecutionEventType } from "@/types";
import { EVENT_TYPE_OPTIONS } from "../hooks/use-event-type-filter";

const chipBase =
  "rounded-full border px-3 py-1 text-xs font-medium transition-colors active:scale-[0.98]";
const chipIdle =
  "border-white/5 bg-white/[0.02] text-zinc-500 hover:text-zinc-300";

// Active chip mirrors the event lifecycle colors of the executions table.
const chipActive: Record<ExecutionEventType, string> = {
  RESERVATION_INTENT: "border-amber-500/40 bg-amber-500/10 text-amber-500",
  RESERVATION_CLAIMED: "border-cyan-400/40 bg-cyan-400/10 text-cyan-400",
  RESERVATION_FAILED: "border-rose-500/40 bg-rose-500/10 text-rose-500",
  ORDER_FILL: "border-emerald-400/40 bg-emerald-400/10 text-emerald-400",
  RESERVATION_RELEASED: "border-zinc-400/40 bg-zinc-400/10 text-zinc-300",
  CREDIT_CLOSED: "border-violet-400/40 bg-violet-400/10 text-violet-400",
  UNCERTAINTY_BOUND_TO_VENUE_OFFER:
    "border-sky-400/40 bg-sky-400/10 text-sky-300",
  UNCERTAINTY_MARKED_NOT_ACCEPTED:
    "border-orange-400/40 bg-orange-400/10 text-orange-300",
  UNCERTAINTY_MANUALLY_RESOLVED:
    "border-fuchsia-400/40 bg-fuchsia-400/10 text-fuchsia-300",
};

interface EventTypeFilterProps {
  value: ExecutionEventType | null;
  onChange: (value: ExecutionEventType | null) => void;
}

/** Chip row filtering the executions browser to a single event type. */
export function EventTypeFilter({ value, onChange }: EventTypeFilterProps) {
  const t = useTranslations("history");

  return (
    <fieldset
      aria-label={t("filterLabel")}
      className="flex flex-wrap gap-2 border-0 p-0"
    >
      <button
        type="button"
        aria-pressed={value === null}
        onClick={() => onChange(null)}
        className={cn(
          chipBase,
          value === null
            ? "border-white/20 bg-white/10 text-zinc-100"
            : chipIdle,
        )}
      >
        {t("filterAll")}
      </button>
      {EVENT_TYPE_OPTIONS.map((type) => (
        <button
          key={type}
          type="button"
          aria-pressed={value === type}
          onClick={() => onChange(type)}
          className={cn(chipBase, value === type ? chipActive[type] : chipIdle)}
        >
          {type}
        </button>
      ))}
    </fieldset>
  );
}
