"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import {
  formatAPR,
  formatDailyRate,
  formatPeriod,
  formatUSD,
} from "@/lib/format";
import type { OfferSummary } from "@/types";

interface OffersListProps {
  offers: OfferSummary[];
}

export function OffersList({ offers }: OffersListProps) {
  const t = useTranslations("overview");
  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
        {t("activeOffers")}
      </h3>

      {offers.length === 0 ? (
        <p className="mt-4 text-sm text-zinc-500">{t("noActiveOffers")}</p>
      ) : (
        <ul className="mt-4 space-y-3">
          {offers.map((offer) => (
            <li key={offer.id} className="flex items-center justify-between">
              <div>
                <p className="font-medium tabular-nums text-foreground">
                  {formatUSD(offer.amount)}
                </p>
                <p className="text-sm">
                  <span className="text-emerald-400 tabular-nums">
                    {formatAPR(offer.rate)}
                  </span>
                  <span className="text-zinc-500">
                    {" "}
                    / {formatDailyRate(offer.rate)}
                    <span className="text-zinc-500 text-xs"> {t("daily")}</span>
                  </span>
                </p>
              </div>
              <Badge variant="secondary">{formatPeriod(offer.period)}</Badge>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
