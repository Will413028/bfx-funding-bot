"use client";

import { useLocale, useTranslations } from "next-intl";
import { DecimalAmount } from "@/components/shared/decimal-amount";
import { Badge } from "@/components/ui/badge";
import { formatRelativeTime } from "@/lib/format";
import type { OfferClaim } from "@/types";

// Lending status colors: queued capital = amber, live on venue = emerald,
// terminal-ok = zinc, terminal-error = rose.
const stateStyles: Record<string, string> = {
  pending: "text-amber-500 border-amber-500/30",
  unknown: "text-orange-400 border-orange-400/30",
  claimed: "text-emerald-400 border-emerald-400/30",
  released: "text-zinc-400 border-zinc-400/30",
  failed: "text-rose-500 border-rose-500/30",
};

interface OffersTableProps {
  offers: OfferClaim[];
}

export function OffersTable({ offers }: OffersTableProps) {
  const t = useTranslations("overview");
  const locale = useLocale();

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("activeOffers")}
      </h3>

      {offers.length === 0 ? (
        <p className="mt-4 text-sm text-zinc-500">{t("noActiveOffers")}</p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <div className="min-w-[480px]">
            <div className="grid grid-cols-[90px_70px_1fr_1fr_1fr] gap-2 border-b border-white/5 pb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">
              <span>{t("state")}</span>
              <span>{t("symbol")}</span>
              <span className="text-right">{t("sizeUsdt")}</span>
              <span className="text-right">{t("placed")}</span>
              <span className="text-right">{t("updated")}</span>
            </div>
            {offers.map((offer) => (
              <div
                key={offer.offerKey}
                className="grid grid-cols-[90px_70px_1fr_1fr_1fr] items-center gap-2 border-b border-white/[0.03] py-2.5 text-sm last:border-0"
              >
                <Badge
                  variant="outline"
                  className={stateStyles[offer.state] ?? "text-zinc-400"}
                >
                  {offer.state}
                </Badge>
                <span className="text-zinc-400">{offer.symbol}</span>
                <span className="text-right font-medium text-foreground">
                  <DecimalAmount value={offer.sizeUsdt} />
                </span>
                <span
                  className="text-right text-xs text-zinc-500"
                  title={new Date(offer.occurredAtMs).toISOString()}
                >
                  {formatRelativeTime(offer.occurredAtMs, locale)}
                </span>
                <span
                  className="text-right text-xs text-zinc-500"
                  title={new Date(offer.lastUpdatedMs).toISOString()}
                >
                  {formatRelativeTime(offer.lastUpdatedMs, locale)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
