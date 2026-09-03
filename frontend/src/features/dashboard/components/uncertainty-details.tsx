"use client";

import { useTranslations } from "next-intl";
import { DecimalAmount } from "@/components/shared/decimal-amount";
import type { Uncertainty } from "@/types";

interface UncertaintyDetailsProps {
  uncertainty: Uncertainty;
}

function actionKey(kind: string): "bind" | "notAccepted" | "manual" {
  if (kind === "submit_outcome_unknown") return "bind";
  if (kind === "unattributed_venue_offer") return "manual";
  return "notAccepted";
}

/** Evidence-only detail view. It contains no venue-submit retry control. */
export function UncertaintyDetails({ uncertainty }: UncertaintyDetailsProps) {
  const t = useTranslations("overview");
  const observedAt = uncertainty.evidenceSummary.observedAtMs;
  const action = actionKey(uncertainty.kind);

  return (
    <details className="mt-2 rounded-lg border border-amber-500/20 bg-black/10 p-3">
      <summary className="cursor-pointer text-xs font-medium text-amber-200">
        {t("uncertaintyDetails")}
      </summary>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
        <div>
          <dt className="text-zinc-500">{t("uncertaintyAmount")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            <DecimalAmount value={uncertainty.intendedAmount} />
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">{t("uncertaintyScope")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            {uncertainty.blockedScope.environment}
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">{t("uncertaintyEvidence")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            {uncertainty.evidenceSummary.outcomeReason ?? uncertainty.kind}
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">{t("uncertaintyObserved")}</dt>
          <dd className="mt-0.5 text-zinc-200">
            {typeof observedAt === "number" ? (
              <time dateTime={new Date(observedAt).toISOString()}>
                {new Date(observedAt).toLocaleString()}
              </time>
            ) : (
              "—"
            )}
          </dd>
        </div>
      </dl>
      <p className="mt-3 text-xs leading-relaxed text-amber-100/80">
        {t(`uncertaintyNextAction.${action}`)}
      </p>
    </details>
  );
}
