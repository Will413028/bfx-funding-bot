import { Download } from "lucide-react";
import { getTranslations } from "next-intl/server";
import { ProofChart } from "@/features/proof/components/proof-chart";
import { ProofDisclaimer } from "@/features/proof/components/proof-disclaimer";
import { ProofReportCard } from "@/features/proof/components/proof-report-card";
import { computeProofStats } from "@/features/proof/lib/compute-proof-stats";
import { getProofSummary } from "@/features/proof/lib/get-proof-summary";

const CSV_SYMBOLS = ["fUST", "fUSD"] as const;

export default async function ProofPage() {
  const t = await getTranslations("proof");
  const summary = await getProofSummary();
  const stats = computeProofStats(summary.weeks);
  const asOfDate = new Date(summary.asOf).toISOString().slice(0, 10);

  return (
    <div className="mx-auto max-w-5xl space-y-10 px-6 py-16">
      <div className="text-center">
        <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-emerald-500/20 bg-emerald-500/5 px-3 py-1 text-emerald-400 text-xs">
          <span className="size-1.5 animate-pulse rounded-full bg-emerald-400" />
          {t("liveBadge")}
        </div>
        <h1 className="font-bold text-4xl tracking-tight sm:text-5xl">
          {t("headline")}
        </h1>
        <p className="mx-auto mt-4 max-w-2xl text-lg text-zinc-400">
          {t("subtitle")}
        </p>
        <p className="mt-3 text-xs text-zinc-600">
          {t("asOf", { date: asOfDate })}
        </p>
      </div>

      {summary.weeks.length === 0 ? (
        <p className="rounded-xl border border-white/5 bg-white/[0.02] py-12 text-center text-sm text-zinc-500">
          {t("empty")}
        </p>
      ) : (
        <>
          <ProofReportCard stats={stats} />
          <ProofChart weeks={summary.weeks} />
        </>
      )}

      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-6 shadow-[inset_0_1px_0_0_rgba(255,255,255,0.1)]">
        <div className="text-center sm:text-left">
          <h3 className="font-semibold text-foreground text-sm">
            {t("csvTitle")}
          </h3>
          <p className="mt-1 text-xs text-zinc-500">{t("csvBody")}</p>
        </div>
        <div className="mt-4 flex flex-col items-center gap-3 sm:flex-row sm:justify-center">
          {CSV_SYMBOLS.map((symbol) => (
            <a
              key={symbol}
              href={`/api/proxy/public/funding-rates.csv?symbol=${symbol}`}
              download
              className="inline-flex shrink-0 items-center gap-2 rounded-lg bg-white/10 px-4 py-2.5 font-medium text-sm text-foreground transition-colors hover:bg-white/15 active:scale-[0.98]"
            >
              <Download className="size-4" />
              {t("csvCta")} ({symbol})
            </a>
          ))}
        </div>
      </div>

      <ProofDisclaimer />
    </div>
  );
}
