"use client";

import { AlertTriangle } from "lucide-react";
import { useTranslations } from "next-intl";
import { UncertaintyDetails } from "@/features/dashboard/components/uncertainty-details";
import type { Uncertainty } from "@/types";

interface UncertaintyBannerProps {
  uncertainties: Uncertainty[];
  /** Symbols in the current dashboard view; unaffected symbols remain listed. */
  visibleSymbols?: string[];
  /** A failed projection read is itself a fail-closed execution warning. */
  isUnavailable?: boolean;
}

/** Symbol-scoped block banner; unrelated symbol cards remain usable. */
export function UncertaintyBanner({
  uncertainties,
  visibleSymbols = [],
  isUnavailable = false,
}: UncertaintyBannerProps) {
  const t = useTranslations("overview");
  const open = uncertainties.filter((u) => u.state === "open");
  const symbols = Array.from(
    new Set([
      ...visibleSymbols,
      ...open.map((uncertainty) => uncertainty.symbol),
    ]),
  );

  if (open.length === 0 && symbols.length === 0 && !isUnavailable) return null;

  const bySymbol = new Map<string, Uncertainty[]>();
  for (const uncertainty of open) {
    const current = bySymbol.get(uncertainty.symbol) ?? [];
    current.push(uncertainty);
    bySymbol.set(uncertainty.symbol, current);
  }

  return (
    <section
      aria-label={
        isUnavailable
          ? t("uncertaintyUnavailableTitle")
          : t("uncertaintyBannerTitle")
      }
      className="rounded-xl border border-amber-500/30 bg-amber-500/[0.07] p-4"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-400" />
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-amber-100">
            {isUnavailable
              ? t("uncertaintyUnavailableTitle")
              : t("uncertaintyBannerTitle")}
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-amber-100/70">
            {isUnavailable
              ? t("uncertaintyUnavailableDescription")
              : t("uncertaintyBannerDescription")}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {symbols.map((symbol) => {
              const scoped = bySymbol.get(symbol) ?? [];
              return (
                <span
                  key={symbol}
                  className={
                    scoped.length > 0
                      ? "rounded-md border border-amber-400/30 bg-amber-400/10 px-2 py-1 text-xs font-medium text-amber-100"
                      : "rounded-md border border-emerald-400/20 bg-emerald-400/5 px-2 py-1 text-xs text-emerald-200"
                  }
                >
                  {symbol}
                  {scoped.length === 0 && (
                    <span className="ml-1 text-emerald-300/70">
                      ({t("uncertaintySymbolClear")})
                    </span>
                  )}
                </span>
              );
            })}
          </div>
          <div className="mt-3 space-y-2">
            {open.map((uncertainty) => (
              <UncertaintyDetails
                key={uncertainty.uncertaintyId}
                uncertainty={uncertainty}
              />
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
