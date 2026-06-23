"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { formatAPR } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { MarketSummary } from "@/types";

interface MarketPanelProps {
  market: MarketSummary | null;
}

export function MarketPanel({ market }: MarketPanelProps) {
  const t = useTranslations("overview");
  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium uppercase tracking-wider text-zinc-400">
          {t("marketSnapshot")}
        </h3>
      </div>

      {!market ? (
        <p className="mt-4 text-sm text-zinc-500">{t("noMarketData")}</p>
      ) : (
        <dl className="mt-4 space-y-3">
          <div className="flex items-center justify-between">
            <dt className="text-sm text-zinc-400">{t("frr")}</dt>
            <dd
              className={cn(
                "font-semibold tabular-nums tracking-tight",
                market.flashFreeze ? "text-rose-500" : "text-emerald-400",
              )}
            >
              {formatAPR(market.frr)}
            </dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-sm text-zinc-400">{t("regime")}</dt>
            <dd>
              <Badge variant="outline">{market.regime}</Badge>
            </dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-sm text-zinc-400">{t("mdcScore")}</dt>
            <dd className="font-medium tabular-nums text-foreground">
              {market.mdcScore.toFixed(2)}
            </dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-sm text-zinc-400">{t("flashFreeze")}</dt>
            <dd
              className={cn(
                "font-medium text-sm",
                market.flashFreeze ? "text-rose-500" : "text-zinc-500",
              )}
            >
              {market.flashFreeze
                ? t("flashFreezeActive")
                : t("flashFreezeNormal")}
            </dd>
          </div>
        </dl>
      )}
    </div>
  );
}
